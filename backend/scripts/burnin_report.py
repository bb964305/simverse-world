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
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

# `python scripts/burnin_report.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.models.llm_usage import LLMUsage  # noqa: E402

# ---------------------------------------------------------------------------
# 预测区间（硬编码，供对账基准；出处见各行注释）
# ---------------------------------------------------------------------------
# E-11 基线定格：15 居民 ≈ $0.88–1.00/天 @ Anthropic Haiku 列表价。
#   出处：docs/research/COST_RESEARCH_REPORT.md §一.1；COST_RESEARCH_LOG.md E-11。
BASELINE_PER_RESIDENT_DAY = (0.88 / 15, 1.00 / 15)  # ≈ $0.0587–0.0667 /居民·天
# 叠加已落地的三大杠杆（E-09/E-10 decide 计划优先跳过 + E-04/E-05 互聊收尾 5→1
# 合并 + E-02 history 双注入修复）后的理论稳态 ≈ 基线的 45%–55%。
#   出处：docs/research/COST_RESEARCH_REPORT.md §一.6（其中 E-09 单项 = 全服省
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
# CLI
# ---------------------------------------------------------------------------
async def _run(days_window: int, residents: int, budget: float | None) -> str:
    from app.config import settings
    from app.database import async_session

    if budget is None:
        budget = settings.budget_global_daily_usd
    since = datetime.now(UTC) - timedelta(days=days_window)
    async with async_session() as session:
        rows = await fetch_rows(session, since)
    return render_report(
        aggregate(rows), residents=residents, budget=budget, window_days=days_window
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="llm_usage burn-in 对账报告（只读）")
    parser.add_argument("--days", type=int, default=2, help="回看窗口天数（默认 2）")
    parser.add_argument("--residents", type=int, default=15,
                        help="活跃居民数，用于 $/居民·天（默认 15；金丝雀阶段填 3-5）")
    parser.add_argument("--budget", type=float, default=None,
                        help="覆盖全局日预算（默认读 settings.budget_global_daily_usd）")
    args = parser.parse_args(argv)
    print(asyncio.run(_run(args.days, max(args.residents, 1), args.budget)))


if __name__ == "__main__":
    main()
