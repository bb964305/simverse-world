#!/usr/bin/env python3
"""Burn-in 成本对账脚本：读 llm_usage，按 UTC 日 × scenario × model 聚合（纯只读）。

用法（vm212 api 容器内跑，DATABASE_URL 已由 deploy compose 注入）::

    docker compose exec api python scripts/burnin_report.py --days 2 --residents 15

本地 / 沙盒（任意 DATABASE_URL）::

    DATABASE_URL=sqlite+aiosqlite:////tmp/bi.db python scripts/burnin_report.py --days 2

输出两部分：
1. 明细表：日 × scenario × model 的 调用数 / tokens / 成本 / parse_ok 率 / attempt>1 占比；
2. 每日汇总：$/居民·天（除以 --residents）、对比 E-11/E-09 预测区间、全局日预算占用 %。

口径注意（与 app/llm/pricing.py 一致）：
- ``cost_usd`` 是 **Anthropic 列表价折算的估计值**；生产端点是百炼中转，真实价目
  未验证（COST_RESEARCH_REPORT.md「五、待 Jimmy」F-02）。对账定版时请同时抄录
  供应商控制台的真实账单，记录两者比值。
- usage 缺失的调用走 ``source=estimated`` 影子计量，token 估计器绝对误差 ±25%。
- 日界与预算熔断（app/llm/budget.py）一致，均为 **UTC 日**（北京时间 -8h）。
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

# `python scripts/burnin_report.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, func  # noqa: E402
import json  # noqa: E402

from app.models.llm_usage import LLMUsage  # noqa: E402
from app.services.civic_membership import (  # noqa: E402
    CIVIC_VOTER_TYPES,
    SIM_RESIDENT_TYPES,
)

# ---------------------------------------------------------------------------
# 预测区间（硬编码，供对账基准；出处见各行注释）
# ---------------------------------------------------------------------------
# E-11 基线定格：15 居民 ≈ $0.88–1.00/天 @ Anthropic Haiku 列表价。
#   出处：archive/2026-07-25/docs/research/COST_RESEARCH_REPORT.md §一.1；
#   COST_RESEARCH_LOG.md E-11。
BASELINE_PER_RESIDENT_DAY = (0.88 / 15, 1.00 / 15)  # ≈ $0.0587–0.0667 /居民·天
# 叠加已落地的三大杠杆（E-09/E-10 decide 计划优先跳过 + E-04/E-05 互聊收尾 5→1
# 合并 + E-02 history 双注入修复）后的理论稳态 ≈ 基线的 45%–55%。
#   出处：archive/2026-07-25/docs/research/COST_RESEARCH_REPORT.md §一.6
#   （其中 E-09 单项 = 全服省
#   29–37%，COST_RESEARCH_LOG.md E-09）。
OPTIMIZED_PER_RESIDENT_DAY = (
    BASELINE_PER_RESIDENT_DAY[0] * 0.45,  # ≈ $0.0264 /居民·天
    BASELINE_PER_RESIDENT_DAY[1] * 0.55,  # ≈ $0.0367 /居民·天
)


# ---------------------------------------------------------------------------
# 聚合（纯函数，供测试直接调用）
# ---------------------------------------------------------------------------
@dataclass
class Group:
    """一个 (日, scenario, model) 桶的累计量。"""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    parse_eval: int = 0  # parse_ok 非 NULL（该调用有 JSON 语义）的行数
    parse_ok: int = 0
    retries: int = 0  # attempt_no > 1 的行数（E-19 重试放大信号）

    def add(self, input_tokens, output_tokens, cost_usd, parse_ok, attempt_no) -> None:
        self.calls += 1
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)
        self.cost_usd += float(cost_usd or 0.0)
        if parse_ok is not None:
            self.parse_eval += 1
            if parse_ok:
                self.parse_ok += 1
        if (attempt_no or 1) > 1:
            self.retries += 1

    @property
    def parse_ok_rate(self) -> float | None:
        """parse_ok 率（仅统计有 JSON 语义的行；无则 None）。"""
        if self.parse_eval == 0:
            return None
        return self.parse_ok / self.parse_eval

    @property
    def retry_share(self) -> float:
        return self.retries / self.calls if self.calls else 0.0


def _day_key(ts: datetime) -> str:
    """UTC 日期键。sqlite 回读的 naive datetime 视为 UTC（写入即 UTC）。"""
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC)
    return ts.date().isoformat()


async def fetch_rows(session, since: datetime):
    """拉取窗口内的明细列（只读，不构造 ORM 对象）。"""
    stmt = (
        select(
            LLMUsage.ts,
            LLMUsage.scenario,
            LLMUsage.model,
            LLMUsage.input_tokens,
            LLMUsage.output_tokens,
            LLMUsage.cost_usd,
            LLMUsage.parse_ok,
            LLMUsage.attempt_no,
        )
        .where(LLMUsage.ts >= since)
        .order_by(LLMUsage.ts)
    )
    return (await session.execute(stmt)).all()


def aggregate(rows) -> dict[str, dict[tuple[str, str], Group]]:
    """rows -> {UTC 日期: {(scenario, model): Group}}。

    rows 的每项形如 (ts, scenario, model, input_tokens, output_tokens,
    cost_usd, parse_ok, attempt_no)。在 Python 侧聚合，规避 sqlite/PG 的
    date()/布尔方言差异（数据量 = 数天遥测，轻松装下）。
    """
    days: dict[str, dict[tuple[str, str], Group]] = {}
    for ts, scenario, model, in_tok, out_tok, cost, parse_ok, attempt_no in rows:
        groups = days.setdefault(_day_key(ts), {})
        g = groups.setdefault((scenario or "?", model or "?"), Group())
        g.add(in_tok, out_tok, cost, parse_ok, attempt_no)
    return days


def summarize_day(groups: dict[tuple[str, str], Group]) -> Group:
    """把一天内所有 (scenario, model) 桶折叠成一日总量。"""
    total = Group()
    for g in groups.values():
        total.calls += g.calls
        total.input_tokens += g.input_tokens
        total.output_tokens += g.output_tokens
        total.cost_usd += g.cost_usd
        total.parse_eval += g.parse_eval
        total.parse_ok += g.parse_ok
        total.retries += g.retries
    return total


# ---------------------------------------------------------------------------
# 渲染（str.format 对齐，无第三方依赖）
# ---------------------------------------------------------------------------
_ROW = "{:<12}{:<16}{:<26}{:>7}{:>11}{:>10}{:>12}{:>10}{:>11}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def classify(value: float, lo: float, hi: float) -> str:
    if value < lo:
        return "低于区间"
    if value > hi:
        return "高于区间"
    return "区间内"


def render_report(
    days: dict[str, dict[tuple[str, str], Group]],
    *,
    residents: int,
    budget: float,
    window_days: int,
    today_key: str | None = None,
) -> str:
    if today_key is None:
        today_key = datetime.now(UTC).date().isoformat()
    out: list[str] = []
    out.append(f"== llm_usage 对账（最近 {window_days} 天，UTC 日界）==")
    out.append("")

    if not days:
        out.append("窗口内没有 llm_usage 行。检查：LLM_METERING_ENABLED=true？"
                    "agent loop 是否在跑（AGENT_ENABLED）？DATABASE_URL 指向对的库？")
        return "\n".join(out)

    out.append(_ROW.format(
        "日期", "scenario", "model", "calls",
        "in_tok", "out_tok", "cost_usd", "parse_ok", "attempt>1",
    ))
    out.append("-" * 115)
    for day in sorted(days):
        groups = days[day]
        ordered = sorted(groups.items(), key=lambda kv: kv[1].cost_usd, reverse=True)
        for (scenario, model), g in ordered:
            out.append(_ROW.format(
                day, scenario[:15], model[:25], g.calls,
                f"{g.input_tokens:,}", f"{g.output_tokens:,}",
                f"${g.cost_usd:.6f}", _pct(g.parse_ok_rate), _pct(g.retry_share),
            ))

    out.append("")
    lo_b, hi_b = BASELINE_PER_RESIDENT_DAY
    lo_o, hi_o = OPTIMIZED_PER_RESIDENT_DAY
    for day in sorted(days):
        t = summarize_day(days[day])
        per_resident = t.cost_usd / residents if residents else 0.0
        partial = "（进行中的一天，数值随时间累积）" if day == today_key else ""
        out.append(f"—— {day} 汇总{partial} ——")
        out.append(f"  调用 {t.calls} | in {t.input_tokens:,} tok | "
                    f"out {t.output_tokens:,} tok | 成本 ${t.cost_usd:.4f}")
        out.append(f"  parse_ok {_pct(t.parse_ok_rate)}（评估 {t.parse_eval} 行）"
                    f" | attempt>1 {_pct(t.retry_share)}")
        out.append(f"  $/居民·天 = ${per_resident:.4f}（--residents {residents}）")
        out.append(f"    vs E-11 基线区间   ${lo_b:.4f}–${hi_b:.4f} → "
                    f"{classify(per_resident, lo_b, hi_b)}"
                    "（出处 COST_RESEARCH_REPORT §一.1）")
        out.append(f"    vs 优化后预期区间 ${lo_o:.4f}–${hi_o:.4f} → "
                    f"{classify(per_resident, lo_o, hi_o)}"
                    "（出处 §一.6：E-09/E-04/E-02 叠加 = 基线的 45–55%）")
        if budget > 0:
            frac = t.cost_usd / budget
            out.append(f"  预算占用 = {frac * 100:.1f}%"
                        f"（BUDGET_GLOBAL_DAILY_USD=${budget:.2f}；"
                        "熔断 80% throttle / 95% rule_only / 100% player_only）")
        else:
            out.append("  预算占用 = n/a（BUDGET_GLOBAL_DAILY_USD=0，熔断关闭）")
        out.append("")

    out.append("注意：cost_usd 为 Anthropic 列表价折算的估计值（百炼中转真实价目未验证，")
    out.append("F-02）；token 估计器 ±25%。定版对账请同时抄录供应商控制台真实账单。")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 拟真探针（realism P0-6 验收）：读移动记忆的 move 面包屑（memorize 写入的
# metadata_json["move"] = {intent, target, moved, arrived}），纯函数聚合。
# ---------------------------------------------------------------------------
async def fetch_move_records(session, since: datetime) -> list[dict]:
    """拉窗口内带 move 面包屑的动作记忆，规约为 probe 用的扁平 dict 列表。"""
    from app.models.memory import Memory
    stmt = (
        select(Memory.resident_id, Memory.content, Memory.metadata_json, Memory.created_at)
        .where(Memory.source == "agent_action", Memory.created_at >= since)
    )
    records: list[dict] = []
    for rid, content, meta, created in (await session.execute(stmt)).all():
        mv = meta.get("move") if isinstance(meta, dict) else None
        if not isinstance(mv, dict):
            continue
        ts = created.astimezone(UTC) if created.tzinfo else created
        records.append({
            "resident_id": rid,
            "content": content or "",
            "day": _day_key(created),
            "hour": ts.hour,
            "target": mv.get("target"),
            "intent": mv.get("intent"),
            "moved": bool(mv.get("moved")),
            "arrived": bool(mv.get("arrived")),
        })
    return records


async def fetch_resident_needs(session) -> list[dict]:
    """当前全体居民的三需求快照（realism P1-13 需求健康度探针）。"""
    from app.models.resident import Resident
    rows = (await session.execute(select(Resident.meta_json))).all()
    out: list[dict] = []
    for (meta,) in rows:
        needs = meta.get("needs") if isinstance(meta, dict) else None
        if isinstance(needs, dict):
            out.append(needs)
    return out


def location_hourly_traffic(records: list[dict]) -> dict[str, dict[int, int]]:
    """地点小时人流曲线：按 category 地点 × 小时统计到达计数（读到达记忆的
    target→category + hour）。期望餐饮出现午晚双峰。"""
    from app.agent.map_data import location_category
    traffic: dict[str, dict[int, int]] = {}
    for r in records:
        if not r["arrived"] or not r["target"]:
            continue
        cat = location_category(r["target"]) or "other"
        traffic.setdefault(cat, {})
        traffic[cat][r["hour"]] = traffic[cat].get(r["hour"], 0) + 1
    return traffic


def needs_health(needs_list: list[dict]) -> dict | None:
    """需求健康度：全体三需求的均值/最低值 + 持续饥饿（satiety<临界）人数。
    死锁信号 = starving 人数高居不下。无居民返回 None。"""
    if not needs_list:
        return None
    from app.config import settings
    keys = ("energy", "satiety", "social")
    stats: dict = {}
    for k in keys:
        vals = [float(n.get(k, 1.0)) for n in needs_list if isinstance(n.get(k), (int, float))]
        if vals:
            stats[k] = {"mean": round(sum(vals) / len(vals), 3), "min": round(min(vals), 3)}
    starving = sum(1 for n in needs_list
                   if isinstance(n.get("satiety"), (int, float))
                   and n["satiety"] < settings.realism_needs_critical)
    stats["starving_count"] = starving
    stats["residents"] = len(needs_list)
    return stats


def plan_arrival_rate(records: list[dict]) -> float | None:
    """计划到达率：VISIT_DISTRICT 计划-地点 trip（resident+target+day 去重）中实际
    到达的比例。修复前 ≈0（计划移动不动），目标 >70%。无 trip 返回 None。"""
    trips: dict[tuple, bool] = {}
    for r in records:
        if r["intent"] != "VISIT_DISTRICT" or not r["target"]:
            continue
        key = (r["resident_id"], r["target"], r["day"])
        trips[key] = trips.get(key, False) or r["arrived"]
    if not trips:
        return None
    return sum(1 for v in trips.values() if v) / len(trips)


def behavior_memory_consistency(records: list[dict]) -> float | None:
    """行为-记忆一致率：移动记忆文本与真实位移状态一致的比例（arrived⇒含"到达"、
    moved⇒含"前往"、未移动⇒不含"到达/前往"）。目标 >95%。无样本返回 None。"""
    if not records:
        return None
    ok = 0
    for r in records:
        c = r["content"]
        if r["arrived"]:
            consistent = "到达" in c
        elif r["moved"]:
            consistent = "前往" in c
        else:
            consistent = ("到达" not in c and "前往" not in c)
        ok += 1 if consistent else 0
    return ok / len(records)


def render_probes(records: list[dict]) -> str:
    out = ["== 拟真探针（P0 验收）=="]
    out.append(f"  计划到达率        = {_pct(plan_arrival_rate(records))}"
               "（VISIT_DISTRICT 计划-地点 trip 到达比例；修复前≈0，目标 >70%）")
    out.append(f"  行为-记忆一致率   = {_pct(behavior_memory_consistency(records))}"
               "（移动记忆文本匹配真实位移；目标 >95%）")
    out.append(f"  样本 = {len(records)} 条移动记忆"
               "（realism 关或无 agent 移动时为 0，探针显示 '-'）")
    return "\n".join(out)


def render_probes_p1(records: list[dict], needs_list: list[dict]) -> str:
    out = ["== 拟真探针（P1 验收）=="]
    traffic = location_hourly_traffic(records)
    if traffic:
        out.append("  地点小时人流（category → {小时:到访数}）：")
        for cat in sorted(traffic):
            hours = ", ".join(f"{h}:{traffic[cat][h]}" for h in sorted(traffic[cat]))
            out.append(f"    {cat:<8} {{{hours}}}")
        out.append("    （期望：dining 午/晚双峰、雨天户外下降）")
    else:
        out.append("  地点小时人流 = -（无到达记忆）")
    nh = needs_health(needs_list)
    if nh:
        parts = ", ".join(
            f"{k}(均{nh[k]['mean']}/低{nh[k]['min']})" for k in ("energy", "satiety", "social") if k in nh)
        out.append(f"  需求健康度 = {parts}")
        out.append(f"    饥饿(satiety<临界)={nh['starving_count']}/{nh['residents']} 人"
                   "（持续高企=需求死锁信号）")
    else:
        out.append("  需求健康度 = -（无居民 needs 数据；realism 关或未起 tick）")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# P2 验收探针（社会结构）：社交网络度分布偏度 + 信息扩散半衰期
# ---------------------------------------------------------------------------
async def fetch_relation_edges(session) -> list[tuple[str, str, float]]:
    """resident_relations 中 familiarity>0.1 的居民-居民边 (a, b, familiarity)。"""
    from app.models.resident_relation import ResidentRelation
    rows = (await session.execute(
        select(ResidentRelation.party_a, ResidentRelation.party_b, ResidentRelation.familiarity,
               ResidentRelation.party_a_type, ResidentRelation.party_b_type)
    )).all()
    return [(a, b, float(fam)) for a, b, fam, at, bt in rows
            if fam is not None and fam > 0.1 and at == "resident" and bt == "resident"]


async def fetch_event_diffusion(session) -> dict:
    """事件记忆（一手 world_event + 二手 gossip，带 event_id）+ 事件类型 + 居民总数。"""
    from app.models.memory import Memory
    from app.models.world_event import WorldEvent
    from app.models.resident import Resident
    ev_type = {eid: t for eid, t in (await session.execute(
        select(WorldEvent.id, WorldEvent.type))).all()}
    records = []
    for rid, meta, created in (await session.execute(
        select(Memory.resident_id, Memory.metadata_json, Memory.created_at)
        .where(Memory.source.in_(("world_event", "gossip")))
    )).all():
        eid = (meta or {}).get("event_id")
        if eid:
            records.append({"event_id": eid, "resident_id": rid, "created_at": created})
    total = (await session.execute(select(func.count()).select_from(Resident))).scalar_one()
    return {"records": records, "event_type": ev_type, "total_residents": total or 0}


def degree_distribution_skewness(edges: list[tuple[str, str, float]]) -> dict | None:
    """居民对话/关系图的度分布 + 偏度（Fisher-Pearson g1，纯 Python）。右偏(>0) =
    存在社交明星与边缘者；关闭开关的对照组因均匀随机应近 0/近对称。无边返回 None。"""
    if not edges:
        return None
    from collections import Counter
    deg: Counter = Counter()
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    degrees = list(deg.values())
    n = len(degrees)
    mean = sum(degrees) / n
    var = sum((d - mean) ** 2 for d in degrees) / n
    std = var ** 0.5
    skew = 0.0 if std == 0 else (sum((d - mean) ** 3 for d in degrees) / n) / (std ** 3)
    return {
        "skewness": round(skew, 3),
        "n_nodes": n,
        "mean_degree": round(mean, 2),
        "max_degree": max(degrees),
        "min_degree": min(degrees),
        "histogram": dict(sorted(Counter(degrees).items())),
    }


def _ts(x):
    return x if x is not None else datetime.min.replace(tzinfo=UTC)


def info_diffusion_half_life(diffusion: dict, exclude_weather: bool = True) -> dict:
    """每个非天气事件：知情居民比例随模拟时间的终值 + 到 50% 的时长（小时）。
    梯度开启时应为数小时量级；对照组(全知广播)所有一手记忆同刻写入 → t50≈0。"""
    from collections import defaultdict
    records = diffusion["records"]
    ev_type = diffusion["event_type"]
    total = diffusion["total_residents"] or 1
    per_event: dict[str, list] = defaultdict(list)
    for r in records:
        if exclude_weather and ev_type.get(r["event_id"]) == "weather":
            continue
        per_event[r["event_id"]].append((r["created_at"], r["resident_id"]))

    events = []
    for eid, entries in per_event.items():
        entries.sort(key=lambda e: _ts(e[0]))
        start = entries[0][0]
        seen: set[str] = set()
        t50 = None
        for ts, rid in entries:
            seen.add(rid)
            if t50 is None and len(seen) / total >= 0.5:
                t50 = ((ts - start).total_seconds() / 3600.0) if (ts and start) else 0.0
        events.append({
            "event_id": eid,
            "informed_count": len(seen),
            "informed_ratio": round(len(seen) / total, 3),
            "time_to_50pct_hours": (round(t50, 3) if t50 is not None else None),
        })
    events.sort(key=lambda e: -e["informed_ratio"])
    return {"events": events, "total_residents": total}


def diffusion_relation_correlation(diffusion: dict, edges: list[tuple[str, str, float]]) -> float | None:
    """抽样验证「知情顺序与关系强度正相关」：取扩散最广的非天气事件，计算被通知
    次序 rank 与该居民对已知情集合的最大 familiarity 的 Pearson 相关。信息若沿强关系
    流动,越晚知情者与先知情者的关系越弱 → 期望负相关。样本不足返回 None。"""
    from collections import defaultdict
    ev_type = diffusion["event_type"]
    per_event: dict[str, list] = defaultdict(list)
    for r in diffusion["records"]:
        if ev_type.get(r["event_id"]) == "weather":
            continue
        per_event[r["event_id"]].append((r["created_at"], r["resident_id"]))
    if not per_event:
        return None
    eid = max(per_event, key=lambda k: len({r for _, r in per_event[k]}))
    entries = sorted(per_event[eid], key=lambda e: _ts(e[0]))
    fam = {}
    for a, b, f in edges:
        fam[(a, b)] = f
        fam[(b, a)] = f
    ranks, strengths = [], []
    informed: list[str] = []
    for i, (_, rid) in enumerate(entries):
        if rid in informed:
            continue
        if informed:  # skip the very first (no prior informed set)
            best = max((fam.get((rid, o), 0.0) for o in informed), default=0.0)
            ranks.append(float(i))
            strengths.append(best)
        informed.append(rid)
    if len(ranks) < 3:
        return None
    n = len(ranks)
    mr, ms = sum(ranks) / n, sum(strengths) / n
    cov = sum((ranks[i] - mr) * (strengths[i] - ms) for i in range(n))
    vr = sum((r - mr) ** 2 for r in ranks) ** 0.5
    vs = sum((s - ms) ** 2 for s in strengths) ** 0.5
    if vr == 0 or vs == 0:
        return None
    return round(cov / (vr * vs), 3)


# --------------------------------------------------------------------------- #
# S1-3 舆论动力学探针（验收：立场方差时间序列收敛/极化，非白噪声）                    #
# --------------------------------------------------------------------------- #

async def fetch_issue_stances(session) -> list[tuple[str, str, float]]:
    """issue_stances 全表 (issue_key, resident_slug, stance) — S1-3 探针输入。"""
    from app.models.issue_stance import IssueStance
    rows = (await session.execute(
        select(IssueStance.issue_key, IssueStance.resident_slug, IssueStance.stance)
    )).all()
    return [(k, s, float(st)) for k, s, st in rows if st is not None]


def opinion_issue_stats(rows: list[tuple[str, str, float]], epsilon: float = 0.4) -> list[dict]:
    """每议题：n / mean / var + 双峰性指标。

    双峰性两个口径（KICKOFF S1-3 §6 允许任一，两个都给）：
    - Sarle bimodality coefficient = (skew²+1)/kurtosis，>5/9≈0.556 → 双峰倾向；
    - ε-最大间隔簇数：stance 排序后按 gap>ε 切簇，2+ = 极化保留。
    方差时间序列 = 连续夜（drift 后）运行本报告采样得到的 var 序列。"""
    from collections import defaultdict
    by_issue: dict[str, list[float]] = defaultdict(list)
    for key, _, st in rows:
        by_issue[key].append(st)
    out = []
    for key, xs in by_issue.items():
        n = len(xs)
        mean = sum(xs) / n
        var = sum((x - mean) ** 2 for x in xs) / n
        std = var ** 0.5
        if std == 0 or n < 3:
            bc = None
        else:
            skew = (sum((x - mean) ** 3 for x in xs) / n) / std ** 3
            kurt = (sum((x - mean) ** 4 for x in xs) / n) / std ** 4
            bc = round((skew ** 2 + 1) / kurt, 3)
        xs_sorted = sorted(xs)
        clusters = 1 if xs_sorted else 0
        for a, b in zip(xs_sorted, xs_sorted[1:]):
            if b - a > epsilon:
                clusters += 1
        out.append({"issue": key, "n": n, "mean": round(mean, 3),
                    "variance": round(var, 4), "bimodality": bc, "clusters": clusters})
    out.sort(key=lambda d: (-d["n"], d["issue"]))
    return out


def render_probes_s13(rows: list[tuple[str, str, float]]) -> str:
    from app.config import settings
    out = ["== 拟真探针（S1-3 验收：议题立场与舆论动力学）=="]
    stats = opinion_issue_stats(rows, epsilon=settings.polis_opinion_epsilon)
    if not stats:
        out.append("  议题立场 = -（issue_stances 空；POLIS_OPINION_ENABLED 关 = "
                   "对照组「无动力学」，或世界尚无辩论/互聊信号）")
        return "\n".join(out)
    out.append(f"  议题数 {len(stats)}（连续夜运行本报告即得方差时间序列；"
               "目标=收敛或极化，非白噪声）")
    for s in stats[:5]:
        bc = "-" if s["bimodality"] is None else s["bimodality"]
        out.append(f"    「{s['issue'][:24]}」 n={s['n']} mean={s['mean']} "
                   f"var={s['variance']} 双峰系数={bc}（>0.556≈双峰） ε-簇数={s['clusters']}")
    return "\n".join(out)


def render_probes_p2(edges: list[tuple[str, str, float]], diffusion: dict) -> str:
    out = ["== 拟真探针（P2 验收：社会结构）=="]
    skew = degree_distribution_skewness(edges)
    if skew:
        out.append(f"  社交网络度分布偏度 = {skew['skewness']}"
                   f"（节点 {skew['n_nodes']}, 均度 {skew['mean_degree']}, "
                   f"度 {skew['min_degree']}–{skew['max_degree']}；目标右偏>0=有社交明星，"
                   "对照组近 0）")
        out.append(f"    度直方图 = {skew['histogram']}")
    else:
        out.append("  社交网络度分布偏度 = -（无 familiarity>0.1 的关系边；"
                   "REALISM_RELATIONS_ENABLED 关或未起 tick）")

    hl = info_diffusion_half_life(diffusion)
    if hl["events"]:
        out.append(f"  信息扩散半衰期（非天气事件，共 {len(hl['events'])} 个，"
                   f"居民 {hl['total_residents']}）：")
        for e in hl["events"][:5]:
            t50 = "未达50%" if e["time_to_50pct_hours"] is None else f"{e['time_to_50pct_hours']}h"
            out.append(f"    {e['event_id'][:12]:<12} 知情 {e['informed_count']} 人"
                       f"（{e['informed_ratio']*100:.0f}%）| 到50%={t50}")
        corr = diffusion_relation_correlation(diffusion, edges)
        if corr is not None:
            out.append(f"    知情顺序×关系强度 Pearson = {corr}"
                       "（负=信息沿强关系先流动，抽样最广事件）")
        out.append("    （目标：t50 数小时量级；对照组全知广播 t50≈0）")
    else:
        out.append("  信息扩散半衰期 = -（无带 event_id 的事件记忆；"
                   "REALISM_INFO_GRADIENT_ENABLED 关或无世界事件）")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# S2-1 §6 — office 快照探针（纯读 offices + system_config + residents.meta_json，
# 零 LLM）。三组指标：职位占用/空缺时序、任期轮替计数（office 行 updated_at 聚
# 合——office_changed WS 事件不落 Outbox，规格允许两径取其一）、镇长身份一致性
# （offices vs system_config['current_mayor'] vs meta_json['mayor'] 持有者集）。
# 开关关时的对照组：只比对后两者，offices 表不参与。
# ---------------------------------------------------------------------------
def _aware(ts):
    """sqlite 裸连接可能回 naive datetime（DB 统一存 UTC）→ 补 UTC tzinfo。"""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


async def fetch_office_snapshot(session) -> dict:
    """offices 行 + config 镇长 + meta_json['mayor'] 持有者集合。offices 表不存
    在（S2-1 前的库）→ available=False，探针整体 fail-open。"""
    import json as _json
    from app.models.office import Office
    snap = {"available": True, "offices": [], "config_mayor": None, "meta_mayors": []}
    try:
        rows = (await session.execute(select(Office).order_by(Office.id))).scalars().all()
        snap["offices"] = [{
            "office_key": o.office_key,
            "holder_slug": o.holder_slug,
            "institution": o.institution,
            "fill_strategy": o.fill_strategy,
            "term_started_at": _aware(o.term_started_at),
            "term_ends_at": _aware(o.term_ends_at),
            "updated_at": _aware(o.updated_at),
        } for o in rows]
    except Exception:
        return {"available": False, "offices": [], "config_mayor": None, "meta_mayors": []}
    try:
        from app.models.system_config import SystemConfig
        row = (await session.execute(
            select(SystemConfig.value).where(SystemConfig.key == "current_mayor")
        )).scalar_one_or_none()
        snap["config_mayor"] = _json.loads(row) if row is not None else None
    except Exception:
        snap["config_mayor"] = None
    try:
        from app.models.resident import Resident
        # C class: probe口径 follows the population set — a stale mayor flag on
        # a player-authored resident must still be visible to the probe.
        pairs = (await session.execute(
            select(Resident.slug, Resident.meta_json).where(
                Resident.resident_type.in_(SIM_RESIDENT_TYPES),
                Resident.meta_json.isnot(None))
        )).all()
        snap["meta_mayors"] = [s for s, m in pairs if (m or {}).get("mayor")]
    except Exception:
        snap["meta_mayors"] = []
    return snap


def office_occupancy(snapshot: dict, now=None) -> list[dict]:
    """每 office 的在任/空缺状态；空缺行给出按 updated_at 推算的空缺天数。"""
    now = now or datetime.now(UTC)
    out = []
    for o in snapshot.get("offices", []):
        occupied = o["holder_slug"] is not None
        vacant_days = None
        if not occupied and o.get("updated_at") is not None:
            vacant_days = max(0, int((now - _aware(o["updated_at"])).total_seconds() // 86400))
        out.append({
            "office_key": o["office_key"],
            "holder_slug": o["holder_slug"],
            "occupied": occupied,
            "vacant_days": vacant_days,
        })
    return out


def office_turnover(snapshot: dict, window_days: int, now=None) -> dict:
    """窗口内被触碰（appoint/vacate → updated_at 刷新）的 office 计数。"""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)
    per = {}
    for o in snapshot.get("offices", []):
        ts = _aware(o.get("updated_at"))
        per[o["office_key"]] = bool(ts is not None and ts >= cutoff)
    return {"changed_in_window": sum(per.values()), "per_office": per}


def mayor_consistency(snapshot: dict, gate_on: bool) -> dict:
    """三存储镇长一致性（不一致 = dual-write bug 告警）。开关关 → 对照组只比对
    system_config vs meta_json 两存储。"""
    office_mayor = None
    for o in snapshot.get("offices", []):
        if o["office_key"] == "mayor":
            office_mayor = o["holder_slug"]
            break
    config_mayor = snapshot.get("config_mayor")
    metas = list(snapshot.get("meta_mayors") or [])
    meta_mayor = metas[0] if len(metas) == 1 else None
    if gate_on:
        consistent = (
            len(metas) <= 1
            and office_mayor == config_mayor
            and (not metas or meta_mayor == office_mayor)
        )
    else:
        consistent = len(metas) <= 1 and (not metas or meta_mayor == config_mayor)
    return {
        "consistent": bool(consistent),
        "office": office_mayor,
        "config": config_mayor,
        "meta": metas,
        "compared_stores": 3 if gate_on else 2,
    }


def render_probes_offices(snapshot: dict, gate_on: bool, window_days: int) -> str:
    out = ["== 社会探针（S2-1 验收：offices 职位实体化）=="]
    if not snapshot.get("available"):
        out.append("  offices 表不存在（迁移未跑或 S2-1 前的库）——探针跳过")
        return "\n".join(out)
    occ = office_occupancy(snapshot)
    if occ:
        out.append("  职位占用/空缺：")
        for o in occ:
            if o["occupied"]:
                out.append(f"    {o['office_key']:<12} 在任 {o['holder_slug']}")
            else:
                days = "?" if o["vacant_days"] is None else str(o["vacant_days"])
                out.append(f"    {o['office_key']:<12} 空缺（{days} 天）")
        out.append("    （目标形态：四职位常态在任，空缺是短暂过渡）")
    else:
        out.append("  职位占用/空缺 = -（offices 表为空——迁移 seed 未跑）")
    t = office_turnover(snapshot, window_days)
    out.append(f"  任期轮替（{window_days} 天窗口，按 office 行 updated_at 聚合）："
               f"{t['changed_in_window']} 个职位有变更 {t['per_office']}")
    out.append("    （目标：镇长随选举周期轮替，文书/邮差稳定）")
    c = mayor_consistency(snapshot, gate_on)
    stores = ("offices/system_config/meta_json 三存储" if gate_on
              else "system_config/meta_json 两存储（对照组，开关关）")
    flag = "一致" if c["consistent"] else "不一致 ⚠️ dual-write bug 告警"
    out.append(f"  镇长身份一致性（{stores}）：{flag}"
               f"（office={c['office']} config={c['config']} meta={c['meta']}）")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# S1-5 §6 — 镇财政探针（纯读 town_treasuries + resident_treasuries +            #
# system_config，零 LLM）。两组指标：                                            #
#  a) 镇财政余额 + 收支流向：本表无流水账（镇流水不进 transactions ledger，见     #
#     models/town_treasury.py），故一次运行 = 时间序列的一个采样点，序列由每日     #
#     重复运行拼出；同时给出货币分布（镇 vs 居民）作为“有界 vs 单调通胀”的判据。   #
#  b) 发薪覆盖率：funded 发薪成功率同样无流水可查，改用可计算的等价代理——          #
#     余额 ÷ 当日应发工资总额 = 财政续航天数（<1 天即进入欠薪风险期）。            #
# 对照组（开关关）：镇余额恒 0、镇占比恒 0、续航恒 0，发薪 100% 靠 MINT。           #
# --------------------------------------------------------------------------- #
async def fetch_treasury_snapshot(session) -> dict:
    """镇账户 + 居民账户总额 + nightly 拨款时间戳 + 在职 duty 工资账单。表不存在
    （S1-5 前的库）→ available=False，探针整体 fail-open。"""
    import json as _json
    snap = {
        "available": True, "town_balance_sc": 0, "town_updated_at": None,
        "resident_total_sc": 0, "resident_accounts": 0,
        "last_spend_at": None, "daily_wage_bill_sc": 0, "duty_holders": 0,
    }
    try:
        from app.models.town_treasury import TOWN_KEY, TownTreasury
        row = (await session.execute(
            select(TownTreasury).where(TownTreasury.key == TOWN_KEY)
        )).scalar_one_or_none()
        if row is not None:
            snap["town_balance_sc"] = row.balance_sc
            snap["town_updated_at"] = _aware(row.updated_at)
    except Exception:
        return {**snap, "available": False}
    try:
        from app.models.resident_treasury import ResidentTreasury
        balances = (await session.execute(
            select(ResidentTreasury.balance_sc))).scalars().all()
        snap["resident_total_sc"] = sum(int(b or 0) for b in balances)
        snap["resident_accounts"] = len(balances)
    except Exception:
        pass
    try:
        from app.models.system_config import SystemConfig
        from app.services.treasury_service import LAST_SPEND_KEY
        raw = (await session.execute(
            select(SystemConfig.value).where(SystemConfig.key == LAST_SPEND_KEY)
        )).scalar_one_or_none()
        snap["last_spend_at"] = _json.loads(raw) if raw is not None else None
    except Exception:
        pass
    try:
        from app.config import settings
        from app.models.resident import Resident
        from app.services import duty_service
        # C class: the wage bill covers everyone who actually holds a duty, so
        # the scan must span the whole population, not just the electorate.
        rows = (await session.execute(
            select(Resident).where(
                Resident.resident_type.in_(SIM_RESIDENT_TYPES),
                Resident.meta_json.isnot(None))
        )).scalars().all()
        bill = 0
        holders = 0
        for r in rows:
            if not duty_service.duty_key(r):
                continue
            holders += 1
            wage = int(duty_service.perk(r, "wage_sc", settings.npc_default_wage_sc))
            if settings.election_enabled and (r.meta_json or {}).get("mayor"):
                wage = int(round(wage * settings.election_mayor_wage_bonus))
            bill += max(0, wage)
        snap["duty_holders"] = holders
        snap["daily_wage_bill_sc"] = bill
    except Exception:
        pass
    return snap



# --------------------------------------------------------------------------- #
# S2-5 §6 — 政策探针（纯读 policies + system_config 探针计数器，零 LLM）。         #
# 两个指标：① 政策漂移距离（按 tier 分档：门槛越高漂移越少；constitutional_core   #
# 恒 0）；② 核心条款触碰计数（尝试数可 >0，成功数恒 = 0）。                        #
# 对照组（POLIS_POLICY_APPROVAL_ENABLED=false）：政策仍是 system_config 无类型      #
# blob，无 tier 约束 → 无分级差异、核心条款无保护。                                #
# --------------------------------------------------------------------------- #
_TIER_ORDER = ("administrative", "simple_majority",
               "absolute_majority", "constitutional_core")


async def fetch_policy_snapshot(session) -> dict:
    """policies 全表 + 核心条款触碰计数器。表不存在（S2-5 前的库 / 迁移未跑）→
    available=False，探针整体 fail-open。"""
    import json as _json
    snap = {"available": True, "policies": [],
            "core_touch": {"attempts": 0, "by_key": {}}}
    try:
        from app.models.policy import Policy
        rows = (await session.execute(
            select(Policy).order_by(Policy.tier, Policy.key))).scalars().all()
        snap["policies"] = [{
            "key": r.key,
            "value": _json.loads(r.value),
            "tier": r.tier,
            "group": r.group,
            "version": r.version,
            "updated_by": r.updated_by,
        } for r in rows]
    except Exception:
        return {"available": False, "policies": [],
                "core_touch": {"attempts": 0, "by_key": {}}}
    try:
        from app.models.system_config import SystemConfig
        from app.services.policy_service import CORE_TOUCH_KEY
        row = (await session.execute(
            select(SystemConfig.value).where(SystemConfig.key == CORE_TOUCH_KEY)
        )).scalar_one_or_none()
        if row is not None:
            parsed = _json.loads(row)
            if isinstance(parsed, dict):
                snap["core_touch"] = {
                    "attempts": int(parsed.get("attempts", 0)),
                    "by_key": dict(parsed.get("by_key") or {}),
                }
    except Exception:
        pass
    return snap


def treasury_money_split(snapshot: dict) -> dict:
    """货币分布：镇 vs 居民。开关关 → 镇占比恒 0（对照组的平线）。"""
    town = int(snapshot.get("town_balance_sc") or 0)
    residents = int(snapshot.get("resident_total_sc") or 0)
    supply = town + residents
    return {
        "town_sc": town,
        "resident_sc": residents,
        "npc_money_supply_sc": supply,
        "town_share": round(town / supply, 4) if supply else 0.0,
    }


def treasury_wage_runway(snapshot: dict) -> dict | None:
    """财政续航：镇余额 ÷ 当日应发工资总额（天）。工资账单为 0（无在职 duty）→
    None，不假造分母。"""
    bill = int(snapshot.get("daily_wage_bill_sc") or 0)
    if bill <= 0:
        return None
    town = int(snapshot.get("town_balance_sc") or 0)
    runway = town / bill
    return {
        "daily_wage_bill_sc": bill,
        "duty_holders": int(snapshot.get("duty_holders") or 0),
        "runway_days": round(runway, 2),
        "at_risk": runway < 1.0,
    }


def render_probes_s15(snapshot: dict, gate_on: bool) -> str:
    out = ["== 拟真探针（S1-5 验收：镇财政闭环）=="]
    if not snapshot.get("available"):
        out.append("  -（town_treasuries 表不存在——迁移未跑）")
        return "\n".join(out)
    split = treasury_money_split(snapshot)
    stamp = snapshot.get("town_updated_at")
    out.append(f"  镇财政余额 = {split['town_sc']} SC"
               f"（最近变动 {stamp.isoformat() if stamp else '-'}；"
               f"本表无流水账，一次运行 = 时间序列一个采样点）")
    out.append(f"  货币分布：镇 {split['town_sc']} / 居民 {split['resident_sc']}"
               f"（{snapshot.get('resident_accounts', 0)} 个账户）"
               f"，NPC 侧货币量 {split['npc_money_supply_sc']} SC，"
               f"镇占比 {split['town_share']}")
    runway = treasury_wage_runway(snapshot)
    if runway:
        flag = " ⚠️ 欠薪风险" if runway["at_risk"] else ""
        out.append(f"  发薪覆盖代理：日工资账单 {runway['daily_wage_bill_sc']} SC"
                   f"（{runway['duty_holders']} 名在职），财政续航 "
                   f"{runway['runway_days']} 天{flag}")
    else:
        out.append("  发薪覆盖代理 = -（无在职 duty，工资账单为 0）")
    out.append(f"  nightly 公共支出最近一次 = {snapshot.get('last_spend_at') or '-'}")
    if gate_on:
        out.append("    （目标形态：余额在税入与薪出之间波动、可为负压力，"
                   "续航偶尔跌破 1 天 = 叙事张力来源）")
    else:
        out.append("    （对照组，开关关：镇余额恒 0、镇占比恒 0、续航恒 0，"
                   "发薪 100% 靠 MINT——货币供给单调增）")
    return "\n".join(out)


def _value_drift(current, seed) -> float:
    """归一化单条漂移量。数值型 → |Δ|/|seed|（seed=0 时取 |Δ|）；
    枚举/布尔/结构型 → 翻转计数（改了记 1，没改记 0）。"""
    if isinstance(current, bool) or isinstance(seed, bool):
        return 0.0 if current == seed else 1.0
    if isinstance(current, (int, float)) and isinstance(seed, (int, float)):
        if seed == 0:
            return abs(float(current))
        return abs(float(current) - float(seed)) / abs(float(seed))
    return 0.0 if current == seed else 1.0


def policy_drift(snapshot: dict) -> dict:
    """政策漂移距离：每条 = amend 次数（version-1）+ 归一化数值漂移；按 tier 聚合。

    目标形态：simple_majority 漂移 > administrative > absolute_majority
    （门槛越高越稳定），constitutional_core **恒为 0**。
    """
    from app.services.policy_service import catalog_default

    per_policy = []
    per_tier: dict[str, dict] = {}
    for p in snapshot.get("policies", []):
        seed = catalog_default(p["key"])
        drift = _value_drift(p["value"], seed) if seed is not None else 0.0
        amends = max(0, int(p["version"]) - 1)
        per_policy.append({
            "key": p["key"], "tier": p["tier"], "version": p["version"],
            "amend_count": amends, "drift": round(drift, 4),
        })
        bucket = per_tier.setdefault(
            p["tier"], {"n": 0, "amend_total": 0, "drift_total": 0.0})
        bucket["n"] += 1
        bucket["amend_total"] += amends
        bucket["drift_total"] += drift
    for bucket in per_tier.values():
        bucket["drift_total"] = round(bucket["drift_total"], 4)
        bucket["drift_mean"] = round(bucket["drift_total"] / bucket["n"], 4) if bucket["n"] else 0.0
    per_policy.sort(key=lambda d: (-d["amend_count"], -d["drift"], d["key"]))
    core = per_tier.get("constitutional_core", {})
    return {
        "per_policy": per_policy,
        "per_tier": per_tier,
        "core_drift": round(float(core.get("drift_total", 0.0)), 4),
        "core_amends": int(core.get("amend_total", 0)),
    }


def core_touch_counts(snapshot: dict) -> dict:
    """核心条款触碰计数：尝试数（探针计数器）与成功数（core 行的 version-1 之和）。

    红线：成功数恒 = 0（PolicyImmutableError 全挡）。对照组（开关关）无
    constitutional_core 概念，成功数 = 尝试数。
    """
    successes = sum(max(0, int(p["version"]) - 1)
                    for p in snapshot.get("policies", [])
                    if p["tier"] == "constitutional_core")
    touch = snapshot.get("core_touch") or {}
    return {
        "attempts": int(touch.get("attempts", 0)),
        "successes": successes,
        "by_key": dict(touch.get("by_key") or {}),
        "core_rows": sum(1 for p in snapshot.get("policies", [])
                         if p["tier"] == "constitutional_core"),
    }


def render_probes_s25(snapshot: dict, gate_on: bool) -> str:
    out = ["== 社会探针（S2-5 验收：政策漂移距离 / 核心条款不可触碰）=="]
    if not snapshot.get("available"):
        out.append("  policies 表不存在（迁移未跑或 S2-5 前的库）——探针跳过")
        return "\n".join(out)
    if not snapshot.get("policies"):
        out.append("  policies 表为空（POLIS_POLICY_ENABLED 关 = 对照组「政策仍是 "
                   "system_config 无类型 blob」，或 seed_defaults 未跑）")
        return "\n".join(out)

    drift = policy_drift(snapshot)
    out.append("  政策漂移距离（按 tier;目标:门槛越高漂移越少，阶梯状累积）：")
    for tier in _TIER_ORDER:
        b = drift["per_tier"].get(tier)
        if not b:
            continue
        out.append(f"    {tier:<20} 条目 {b['n']:>2} | amend 累计 {b['amend_total']:>3} "
                   f"| 漂移合计 {b['drift_total']} (均值 {b['drift_mean']})")
    out.append("    漂移最大的条目：" + (
        ", ".join(f"{d['key']}(v{d['version']}, Δ{d['drift']})"
                  for d in drift["per_policy"][:3]) or "-"))

    touch = core_touch_counts(snapshot)
    verdict = ("成功数 = 0 ✅ 核心条款不可触碰"
               if touch["successes"] == 0
               else f"成功数 = {touch['successes']} 🔴 红线破防：宪法核心被改动")
    out.append(f"  核心条款触碰计数:尝试 {touch['attempts']} 次 / {verdict}"
               f"（核心条目 {touch['core_rows']} 条,漂移合计 {drift['core_drift']}）")
    if touch["by_key"]:
        top = sorted(touch["by_key"].items(), key=lambda kv: -kv[1])[:3]
        out.append("    被盯上的核心条款：" +
                   ", ".join(f"{k}×{v}" for k, v in top))
    if not gate_on:
        out.append("    （对照组:POLIS_POLICY_APPROVAL_ENABLED 关 = 无分级纪律，"
                   "任意 admin 可改任意键，漂移应呈无差别随机游走）")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# NPC 投票分布探针（fix/npc-choice-option0 验收复核）
# ---------------------------------------------------------------------------
# ops-audit-2026-07-25B §A 只能靠手写 SQL 取数（现网 3 张 poll 全部 14/14 压
# option 0，H/lnK = 0）。这个探针把同一口径固化下来，让下一次运维审计直接读数
# 复核 option-0 偏向是否真的修好了。纯只读、零 LLM。
# 判据里的「option-0 占比 ≤ 45%」是对 K=4 形态写的；跨不同 K 的 poll 聚合时
# 直接比原始占比不成立（K=2 的无偏基线本来就是 50%），所以聚合口径用**超额偏向
# 指数** mean(share0 - 1/K)：无偏世界 ≈ 0，修复前的现网 = 0.5~0.75。
NPC_VOTE_BIAS_GATE = 0.15      # 判据：超额偏向指数 ≤ 0.15
NPC_VOTE_OPTION0_GATE = 0.45   # 判据：单张 K≥4 的 poll，option-0 占比 ≤ 45%
NPC_VOTE_ENTROPY_GATE = 0.60   # 判据：归一化熵 H/lnK ≥ 0.60（K≥2 的 poll）


async def fetch_poll_vote_snapshot(session, limit: int = 10) -> dict:
    """最近 ``limit`` 张 poll 的 NPC 票分布。表不存在 → available=False。"""
    try:
        from app.models.season import Poll
        rows = (await session.execute(
            select(Poll).order_by(Poll.closes_at.desc()).limit(limit)
        )).scalars().all()
    except Exception:
        return {"available": False, "polls": []}
    polls = []
    for p in rows:
        opts = list(p.options_json or [])
        if not opts:
            continue
        polls.append({
            "id": str(p.id),
            "question": (p.question or "")[:28],
            "status": p.status,
            "closes_at": p.closes_at,
            "tally": [int(o.get("npc_votes", 0) or 0) for o in opts],
            "voters": len((opts[0] or {}).get("_npc_voters") or []),
        })
    return {"available": True, "polls": polls}


def _norm_entropy(tally: list[int]) -> float:
    """归一化香农熵 H/lnK。单选项 poll（K<2）无定义，记 0。"""
    total = sum(tally)
    if total <= 0 or len(tally) < 2:
        return 0.0
    h = -sum((n / total) * math.log(n / total) for n in tally if n > 0)
    return abs(h / math.log(len(tally)))  # abs(): 单选项独占时 -sum(0.0) = -0.0


def npc_vote_distribution(snapshot: dict) -> dict:
    """每张 poll 的 option-0 占比 / 归一化熵，加全局聚合。

    目标形态：option-0 占比在 1/K 附近波动、H/lnK 接近 1、monopoly_polls = 0。
    坏形态（修复前的现网）：option-0 占比 = 1.0、H/lnK = 0、monopoly = 全部。
    """
    per_poll = []
    votes_total = 0
    votes_on_0 = 0
    monopoly = 0
    for p in snapshot.get("polls", []):
        tally = p["tally"]
        total = sum(tally)
        if total <= 0:
            continue
        share0 = tally[0] / total
        per_poll.append({
            "id": p["id"], "question": p["question"], "status": p["status"],
            "k": len(tally), "tally": tally, "votes": total,
            "share0": round(share0, 4),
            "uniform": round(1 / len(tally), 4),
            "excess0": round(share0 - 1 / len(tally), 4),
            "entropy": round(_norm_entropy(tally), 4),
            "nonzero": sum(1 for n in tally if n > 0),
        })
        votes_total += total
        votes_on_0 += tally[0]
        if len(tally) >= 2 and max(tally) == total:
            monopoly += 1
    n = len(per_poll)
    return {
        "per_poll": per_poll,
        "polls_with_votes": n,
        "votes_total": votes_total,
        "share0_overall": round(votes_on_0 / votes_total, 4) if votes_total else 0.0,
        "bias_index": round(sum(d["excess0"] for d in per_poll) / n, 4) if n else 0.0,
        "entropy_mean": round(sum(d["entropy"] for d in per_poll) / n, 4) if n else 0.0,
        "monopoly_polls": monopoly,
    }


def render_probes_npc_vote(snapshot: dict, legacy_on: bool, limit: int) -> str:
    out = [f"== 治理探针（NPC 投票分布 · 最近 {limit} 张 poll）=="]
    if not snapshot.get("available"):
        out.append("  polls 表不存在（迁移未跑）——探针跳过")
        return "\n".join(out)
    dist = npc_vote_distribution(snapshot)
    if not dist["polls_with_votes"]:
        out.append("  窗口内没有已投票的 poll（NPC 票合计 0）——无样本")
        return "\n".join(out)

    verdict0 = ("✅" if dist["bias_index"] <= NPC_VOTE_BIAS_GATE
                else "🔴 option-0 结构性偏向")
    verdictH = ("✅" if dist["entropy_mean"] >= NPC_VOTE_ENTROPY_GATE else "🔴 分布过于集中")
    out.append(f"  样本:{dist['polls_with_votes']} 张 poll / {dist['votes_total']} 张 NPC 票")
    out.append(f"  option-0 超额偏向指数 mean(占比-1/K) = {dist['bias_index']:+.4f}"
               f"（门槛 ≤ {NPC_VOTE_BIAS_GATE}）{verdict0}"
               f"；原始 option-0 总占比 = {dist['share0_overall']:.1%}")
    out.append(f"  归一化熵 H/lnK 均值 = {dist['entropy_mean']}"
               f"（门槛 ≥ {NPC_VOTE_ENTROPY_GATE}）{verdictH}")
    mono = dist["monopoly_polls"]
    out.append(f"  全票压单一选项的 poll = {mono} / {dist['polls_with_votes']}"
               + ("（✅ 无垄断）" if mono == 0 else "（🔴 垄断仍在）"))
    out.append("  逐张明细（tally / option-0 占比 vs 无偏基线 1/K / H·lnK⁻¹）：")
    for d in dist["per_poll"]:
        flag = " 🔴" if d["k"] >= 4 and d["share0"] > NPC_VOTE_OPTION0_GATE else ""
        out.append(f"    [{d['status']:<6}] {d['question']:<28} K={d['k']} "
                   f"{d['tally']} → {d['share0']:.1%} vs {d['uniform']:.1%} "
                   f"/ {d['entropy']} (非零选项 {d['nonzero']}/{d['k']}){flag}")
    if legacy_on:
        out.append("    ⚠️ CIVIC_NPC_CHOICE_LEGACY=true —— 跑的是修复前的旧评分器，"
                   "option-0 占比预期回到 ~100%,本探针的门槛不适用")
    else:
        out.append("    （口径同 ops-audit-2026-07-25B §A：NPC 票取自 "
                   "options_json[i].npc_votes,不含 votes 表的玩家票）")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 政治层边界探针（线 A hotfix-4）—— 只读、零 LLM
# ---------------------------------------------------------------------------
#: 刻意落在两个集合之外、且已有专门谓词家族管辖的取值。"player" 是玩家化身
#: （单值 FK users.player_resident_id），由 `!= "player"` 家族管，不算「没人管」。
_BOUNDARY_KNOWN_OUTSIDE = frozenset({"player"})


async def fetch_civic_boundary_snapshot(session) -> dict:
    """按 resident_type 分组的居民计数。residents 表不存在 → available=False。"""
    try:
        from app.models.resident import Resident
        rows = (await session.execute(
            select(Resident.resident_type, func.count())
            .group_by(Resident.resident_type)
        )).all()
    except Exception:
        return {"available": False, "by_type": {}}
    return {"available": True,
            "by_type": {(t or "<null>"): int(n or 0) for t, n in rows}}


def civic_boundary_breakdown(
    snapshot: dict,
    voter_types: frozenset[str] | None = None,
    population_types: frozenset[str] | None = None,
) -> dict:
    """每个 resident_type 的「总数 / 进政治层 / 进世界人口」三列。

    07-25 审计是人工发现「夜风侦探」×3 拿到了投票权的。这个探针把同一个发现
    自动化:UGC 取值出现在投票人列里 = 泄漏复发;某个取值同时落在两列之外
    = 没有任何代码在管这批居民。

    ``voter_types`` / ``population_types`` 可注入,便于测试与「假如把某个取值
    并入政治层」的推演;默认读生产常量。
    """
    voters_set = CIVIC_VOTER_TYPES if voter_types is None else voter_types
    pop_set = SIM_RESIDENT_TYPES if population_types is None else population_types
    by_type = snapshot.get("by_type", {})

    per_type = []
    total = voters = population = outside = 0
    unknown: dict[str, int] = {}
    leaked: list[str] = []
    for rtype, n in sorted(by_type.items()):
        is_voter = rtype in voters_set
        in_pop = rtype in pop_set
        total += n
        if is_voter:
            voters += n
        if in_pop:
            population += n
        if not is_voter and not in_pop:
            outside += n
            # "player" 落在两列之外是设计:它是玩家化身,由第三个谓词家族
            # (`!= "player"`) 管辖,不是「没人管的居民」。其它取值才要报。
            if rtype not in _BOUNDARY_KNOWN_OUTSIDE:
                unknown[rtype] = n
        # UGC 取值拿到票 = 本次 hotfix 的回归;"npc" 有票是设计。
        if is_voter and rtype not in ("npc",):
            leaked.append(rtype)
        per_type.append({"type": rtype, "count": n,
                         "voter": is_voter, "population": in_pop})
    return {
        "per_type": per_type,
        "total": total,
        "voters": voters,
        "population": population,
        # 两列之外的居民数（既无票也不算世界人口）——今天是 "player"（设计）
        # 与 "preset"（待决项）。
        "outside_both": outside,
        "unknown_types": unknown,
        "leaked_voter_types": leaked,
    }


def render_probes_civic_boundary(
    snapshot: dict,
    voter_types: frozenset[str] | None = None,
    population_types: frozenset[str] | None = None,
) -> str:
    out = ["== 政治层边界探针（按 resident_type · 只读零 LLM）=="]
    if not snapshot.get("available"):
        out.append("  residents 表不存在（迁移未跑）——探针跳过")
        return "\n".join(out)
    d = civic_boundary_breakdown(snapshot, voter_types, population_types)
    if not d["total"]:
        out.append("  世界里还没有居民——无样本")
        return "\n".join(out)

    out.append(f"  居民合计 {d['total']}；有政治权利 {d['voters']}"
               f"（CIVIC_VOTER_TYPES）；算世界人口 {d['population']}"
               f"（SIM_RESIDENT_TYPES）")
    out.append("  逐 type 明细（票 = 可投票/可参选，人 = 进世界人口口径）：")
    for row in d["per_type"]:
        vote = "票✅" if row["voter"] else "票—"
        pop = "人✅" if row["population"] else "人—"
        flag = " 🔴 UGC 取值拿到了投票权（泄漏复发）" if row["type"] in d["leaked_voter_types"] else ""
        out.append(f"    {row['type']:<10} {row['count']:>5}  {vote}  {pop}{flag}")
    if d["leaked_voter_types"]:
        out.append("  🔴 玩家创作的居民重新获得了政治权利——回归到 07-25 的泄漏状态，"
                   "查 CIVIC_VOTER_TYPES 与 5 处创建路径")
    if d["unknown_types"]:
        # F2：从 ⚠️ 升为 🔴。这是未来引入新 resident_type 取值时唯一的自动
        # 发现口，也是「写错一个字符（"npc "）就同时掉出两个集合」的唯一兜底
        # ——写错的那一位居民会从 agent loop、市政厅名册、职务查找与 mayor
        # 清扫里一起消失，除了这一行没有任何地方会喊。
        out.append(f"  🔴 两列之外的取值 {d['unknown_types']}"
                   "——既不投票也不算世界人口。'preset'（admin 创建）是已知的"
                   "待决项;其它取值一律按事故处理:查 5 处创建路径与 "
                   "_BOUNDARY_KNOWN_OUTSIDE，并同步 SIM_RESIDENT_TYPES 的决定")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# F2 公民权档位探针（晋升/撤销可观测）—— 只读、零 LLM
# ---------------------------------------------------------------------------
#
# 现有的政治层边界探针判泄漏的条件是「常量集合被拓宽」（civic_boundary_
# breakdown），而 F2 只改行值不改集合，那条永远不会触发；误升只会让 npc 计数
# 合法增长，07-25 靠人眼看出「npc 该是 10 人却有 13 人」的嗅觉也一起失效。
# 所以这里把判据改成「provenance=UGC 且 is_civic_voter 为真、但
# civic_standing_history 里查不到晋升记录」。

#: 「最近 N 世界日内发生翻转」的窗口。滞后设计生效后稳态下这个数应恒为 0，
#: 所以它是**告警条件**，不是信息项。
CIVIC_FLIP_WINDOW_WORLD_DAYS = 7.0


async def fetch_civic_standing_snapshot(
    session, *, gate_office_on: bool = False,
    flip_window_world_days: float = CIVIC_FLIP_WINDOW_WORLD_DAYS,
) -> dict:
    """交叉表 / 晋升队列 / 翻转统计 / 交叉一致性。表不存在 → available=False。"""
    from datetime import timedelta

    try:
        from app.models.civic_standing_history import CivicStandingHistory
        from app.models.office import Office
        from app.models.resident import Resident
        from app.models.season import Poll
        from app.models.system_config import SystemConfig
        from app.services.civic_membership import (
            CITIZEN, CIVIC_VOTER_TYPES as _VOTERS, POLITICAL_FILL_STRATEGY,
            SYSTEM_CREATOR_ID, UGC_RESIDENT_TYPE, is_ugc_resident,
            min_familiarity, min_peers, min_tenure_world_days, min_world_days,
            peer_seasoning_world_days, promotion_cooldown_world_days,
        )
        from app.tasks import civic_promotion as cp

        residents = (await session.execute(select(Resident))).scalars().all()
        history = (await session.execute(
            select(CivicStandingHistory))).scalars().all()
        offices = (await session.execute(select(Office))).scalars().all()
        polls = (await session.execute(
            select(Poll).where(Poll.status == "open"))).scalars().all()
        mayor_cfg_raw = (await session.execute(
            select(SystemConfig.value)
            .where(SystemConfig.key == "current_mayor"))).scalar_one_or_none()
    except Exception:
        return {"available": False}

    by_id = {r.id: r for r in residents}
    voter_slugs = {r.slug for r in residents if r.resident_type in _VOTERS}
    promoted_ids = {h.resident_id for h in history if h.new_standing == CITIZEN}

    cross = {"builtin_citizen": 0, "ugc_citizen_promoted": 0,
             "ugc_citizen_unrecorded": 0, "ugc_denizen": 0, "other": 0}
    leaked: list[str] = []
    for r in residents:
        is_voter = r.resident_type in _VOTERS
        if r.creator_id == SYSTEM_CREATOR_ID and is_voter:
            cross["builtin_citizen"] += 1
        elif is_ugc_resident(r) and is_voter:
            if r.id in promoted_ids:
                cross["ugc_citizen_promoted"] += 1
            else:
                cross["ugc_citizen_unrecorded"] += 1
                leaked.append(r.slug)
        elif is_ugc_resident(r) and r.resident_type == UGC_RESIDENT_TYPE:
            cross["ugc_denizen"] += 1
        else:
            cross["other"] += 1

    # ② 晋升队列（= shadow 模式的候选名单大小）
    try:
        snap = await cp.build_snapshot(session)
        queue_ids = cp.select_promotions(
            snap, min_world_days=min_world_days(), min_peers=min_peers(),
            min_familiarity=min_familiarity(),
            seasoning_days=peer_seasoning_world_days())
        queue = {"size": len(queue_ids),
                 "slugs": sorted(by_id[i].slug for i in queue_ids
                                 if i in by_id)}
        now_world = snap.now_world
    except Exception:
        queue = {"size": None, "slugs": []}
        from app import world_clock
        now_world = world_clock.now_world()

    # ③ 翻转统计
    changes: dict[str, int] = {}
    recent: set[str] = set()
    last_change: dict[str, object] = {}
    for h in history:
        changes[h.resident_id] = changes.get(h.resident_id, 0) + 1
        when = cp._as_aware(h.world_at)
        if (now_world - when) <= timedelta(days=flip_window_world_days):
            recent.add(h.resident_id)
        prev = last_change.get(h.resident_id)
        if prev is None or when > prev[0]:
            last_change[h.resident_id] = (when, h.new_standing)
    in_min_tenure = sum(
        1 for (when, new) in last_change.values()
        if new == CITIZEN
        and (now_world - when) < timedelta(days=min_tenure_world_days()))
    in_cooldown = sum(
        1 for (when, new) in last_change.values()
        if new != CITIZEN
        and (now_world - when) < timedelta(days=promotion_cooldown_world_days()))
    flips = {
        "window_world_days": flip_window_world_days,
        "residents_with_history": len(changes),
        "max_changes_per_resident": max(changes.values()) if changes else 0,
        "recent_flip_residents": len(recent),
        "in_min_tenure": in_min_tenure,
        "in_cooldown": in_cooldown,
    }

    # ④ 交叉一致性
    resident_slugs = {r.slug for r in residents}
    election_office_non_voter = [
        [o.office_key, o.holder_slug] for o in offices
        if o.fill_strategy == POLITICAL_FILL_STRATEGY and o.holder_slug
        and o.holder_slug not in voter_slugs
    ]
    dangling = sorted({o.holder_slug for o in offices
                       if o.holder_slug and o.holder_slug not in resident_slugs})
    meta_mayors = sorted(r.slug for r in residents
                         if (r.meta_json or {}).get("mayor"))
    office_mayor = next((o.holder_slug for o in offices
                         if o.office_key == "mayor"), None)
    cfg_mayor = None
    if mayor_cfg_raw is not None:
        try:
            cfg_mayor = json.loads(mayor_cfg_raw)
        except (TypeError, ValueError):
            cfg_mayor = None
    # ⚠️ 按 polis_office_enabled 分档：gate 关时 offices 是迁移 046 的遗留值，
    # 不分档会在 T2 前直接报红并被当噪声关掉。
    mayor_reps = {
        "checked": bool(gate_office_on),
        "meta": meta_mayors,
        "office": office_mayor,
        "config": cfg_mayor,
        "consistent": None,
    }
    if gate_office_on:
        reps = {tuple(meta_mayors),
                tuple([office_mayor] if office_mayor else []),
                tuple([cfg_mayor] if cfg_mayor else [])}
        mayor_reps["consistent"] = len(reps) == 1

    ghost_votes = []
    for poll in polls:
        opts = list(poll.options_json or [])
        if not opts:
            continue
        voters = list((opts[0] or {}).get("_npc_voters", []))
        ghosts = sorted(s for s in voters if s not in voter_slugs)
        if ghosts:
            ghost_votes.append({"question": poll.question,
                                "ghosts": len(ghosts), "slugs": ghosts})

    return {
        "available": True,
        "cross": cross,
        "leaked": sorted(leaked),
        "queue": queue,
        "flips": flips,
        "crosscheck": {
            "election_office_non_voter": election_office_non_voter,
            "mayor_reps": mayor_reps,
            "ghost_votes": ghost_votes,
            "dangling_holders": dangling,
        },
    }


def render_probes_civic_standing(snapshot: dict) -> str:
    out = ["== 公民权档位探针（provenance × standing · 只读零 LLM）=="]
    if not snapshot.get("available"):
        out.append("  civic_standing_history 表不存在（迁移未跑）——探针跳过")
        return "\n".join(out)

    c = snapshot["cross"]
    out.append(f"  内置公民 {c['builtin_citizen']}；已晋升 UGC 公民 "
               f"{c['ugc_citizen_promoted']}；未晋升 UGC 居民 {c['ugc_denizen']}；"
               f"其它（player/preset）{c['other']}")
    if snapshot["leaked"]:
        out.append(f"  🔴 provenance=UGC 且有投票权、但查不到晋升记录："
                   f"{snapshot['leaked']}")
        out.append("     —— 要么是泄漏复发，要么是 admin 手工改回了 npc"
                   "（后者是有用的红旗，不是噪声）")
    else:
        out.append("  ✅ 每一位有投票权的 UGC 居民都有对应的晋升记录")

    q = snapshot["queue"]
    if q["size"] is None:
        out.append("  晋升队列：计算失败（关系表或 world_clock 不可用）")
    else:
        out.append(f"  晋升队列（满足门槛但仍是 denizen）：{q['size']} 人 "
                   f"{q['slugs'][:20]}")

    f = snapshot["flips"]
    flip_flag = "🔴" if f["recent_flip_residents"] > 0 else "✅"
    out.append(f"  翻转统计：有档位历史的居民 {f['residents_with_history']}；"
               f"单人最多变更 {f['max_changes_per_resident']} 次")
    out.append(f"  {flip_flag} 最近 {f['window_world_days']:.0f} 世界日内发生"
               f"翻转的居民 = {f['recent_flip_residents']}"
               "（滞后设计生效后稳态应恒为 0，>0 是告警不是信息）")
    out.append(f"  当前处于最短任期内 {f['in_min_tenure']} 人 / 冷却期内 "
               f"{f['in_cooldown']} 人")

    x = snapshot["crosscheck"]
    if x["election_office_non_voter"]:
        out.append(f"  🔴 民选职位被非公民占据：{x['election_office_non_voter']}"
                   "（只对 fill_strategy='election' 断言；劳动职务不算）")
    else:
        out.append("  ✅ 民选职位的在任者都持有政治权利")
    mr = x["mayor_reps"]
    if not mr["checked"]:
        out.append("  ⏸ 三处镇长表示一致性：polis_office_enabled=False，"
                   "offices 可能是迁移 046 的遗留值——本档不判定")
    elif mr["consistent"]:
        out.append(f"  ✅ 三处镇长表示一致（meta={mr['meta']}）")
    else:
        out.append(f"  🔴 三处镇长表示分歧：meta={mr['meta']} / "
                   f"offices={mr['office']!r} / config={mr['config']!r}")
    if x["ghost_votes"]:
        out.append("  ⚠️ 幽灵票（投票时具备资格即计票，是设计语义不是 bug）：")
        for g in x["ghost_votes"]:
            out.append(f"    {g['question'][:28]:<28} {g['ghosts']} 张 "
                       f"{g['slugs'][:10]}")
    if x["dangling_holders"]:
        out.append(f"  🔴 offices.holder_slug 在 residents 表里查不到："
                   f"{x['dangling_holders']}"
                   "（purge_residents 不清 offices 与 current_mayor）")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _run(days_window: int, residents: int, budget: float | None,
               polls_window: int = 10) -> str:
    from app.config import settings
    from app.database import async_session

    if budget is None:
        budget = settings.budget_global_daily_usd
    since = datetime.now(UTC) - timedelta(days=days_window)
    async with async_session() as session:
        rows = await fetch_rows(session, since)
        move_records = await fetch_move_records(session, since)
        resident_needs = await fetch_resident_needs(session)
        rel_edges = await fetch_relation_edges(session)
        diffusion = await fetch_event_diffusion(session)
        office_snap = await fetch_office_snapshot(session)
        stance_rows = await fetch_issue_stances(session)
        treasury_snap = await fetch_treasury_snapshot(session)
        policy_snap = await fetch_policy_snapshot(session)
        poll_snap = await fetch_poll_vote_snapshot(session, limit=polls_window)
        boundary_snap = await fetch_civic_boundary_snapshot(session)
        standing_snap = await fetch_civic_standing_snapshot(
            session, gate_office_on=settings.polis_office_enabled)
    report = render_report(
        aggregate(rows), residents=residents, budget=budget, window_days=days_window
    )
    return (report + "\n\n" + render_probes(move_records)
            + "\n\n" + render_probes_p1(move_records, resident_needs)
            + "\n\n" + render_probes_p2(rel_edges, diffusion)
            + "\n\n" + render_probes_offices(
                office_snap, gate_on=settings.polis_office_enabled,
                window_days=days_window)
            + "\n\n" + render_probes_s13(stance_rows)
            + "\n\n" + render_probes_s15(
                treasury_snap, gate_on=settings.town_treasury_enabled)
            + "\n\n" + render_probes_s25(
                policy_snap, gate_on=settings.polis_policy_approval_enabled)
            + "\n\n" + render_probes_npc_vote(
                poll_snap, legacy_on=settings.civic_npc_choice_legacy,
                limit=polls_window)
            + "\n\n" + render_probes_civic_boundary(boundary_snap)
            + "\n\n" + render_probes_civic_standing(standing_snap))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="llm_usage burn-in 对账报告（只读）")
    parser.add_argument("--days", type=int, default=2, help="回看窗口天数（默认 2）")
    parser.add_argument("--residents", type=int, default=15,
                        help="活跃居民数，用于 $/居民·天（默认 15；金丝雀阶段填 3-5）")
    parser.add_argument("--budget", type=float, default=None,
                        help="覆盖全局日预算（默认读 settings.budget_global_daily_usd）")
    parser.add_argument("--polls", type=int, default=10,
                        help="NPC 投票分布探针回看的 poll 张数（默认最近 10 张）")
    args = parser.parse_args(argv)
    print(asyncio.run(_run(args.days, max(args.residents, 1), args.budget,
                           max(args.polls, 1))))


if __name__ == "__main__":
    main()
