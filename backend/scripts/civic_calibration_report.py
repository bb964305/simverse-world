#!/usr/bin/env python3
"""F2 三个门槛的**只读标定报告**（spec §4.2 的「F2 第一步」）。

用法（vm212 api 容器内跑，DATABASE_URL 已由 deploy compose 注入）::

    docker compose exec api python scripts/civic_calibration_report.py --list

本机 dev 库::

    DATABASE_URL=sqlite+aiosqlite:////tmp/f2.db python scripts/civic_calibration_report.py

**阈值必须由实测分布决定，不能拍脑袋。** ``rep_credit_min_score = -0.3`` 之所以
变成装饰性闸门，正是因为它是拍出来的；F2 的三个门槛
（``CIVIC_PROMOTION_MIN_WORLD_DAYS`` / ``MIN_PEERS`` / ``MIN_FAMILIARITY``）在
``civic_membership`` 里给的是**占位默认值，标定前不得开闸**。

判据是「使晋升面**非空且非全量**」：
- 空  → 阈值写紧了（或 familiarity 的主增长路径没开，见 ``REALISM_RELATIONS_ENABLED``）
- 全量 → 阈值写松了，开闸当晚会整批放行

**但「形状对了」不等于「三道闸都生效」。** 姊妹线 F1 踩过的坑：光有分数/计数
分布无法区分「机制生效」与「全部落在边界情形上」。所以报告在三张表之外还给
两样东西，缺一样这份报告就无法回答「门槛设对了没有」：

- **门槛归因**（:func:`attribute_gates`）——当前阈值下*每道闸各拒了谁*。拒绝面
  为 0 的闸会被点名为**装饰性**：那正是 ``rep_credit_min_score = -0.3`` 的失效
  签名（闸门在，拒绝面 0/13），而 verdict=partial 完全掩盖得住它。
- **实测扫描**（:func:`sweep_thresholds`）——候选阈值**全部由上面的分布推出**
  （nearest-rank 分位数 + 向下取整，保证「产生这个候选值的那位居民自己能过」），
  逐组用 ``select_promotions`` 本尊算晋升面，直接答出「哪几组取值使晋升面非空
  且非全量」。没有这一步，报告只能说「写松了/写紧了」，说不出该改成多少——
  而阈值恰恰是「不许拍数字」的那三个。空库 → 零候选值：候选值只能是量出来的。

三条实现约束：

1. **复用** ``civic_promotion.build_snapshot`` / ``select_promotions``，不另写一份
   查询——标定读数与夜间任务判据必须逐字同源，两边各写一份必然漂移。
2. **零写入**。没有 ``--dry-run``：整个脚本只有 dry 一态，它是要跑在生产库上的。
3. **读数为空必须自己喊**。本机 dev 库是空的；``needs_production_recalibration``
   为真时报告里会出现「待生产数据复标」——这行就是交给收口会话的交付物
   （spec §4.2 的降级路径：以 dev 库标定必须显式标注，不得直接开闸）。

⚠️ 内置阵容的世界龄已 ≈450 世界日、UGC 新人从 0 开始，**两类人不要放进同一
分布看**——所以表①只统计 UGC denizen。
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from datetime import timedelta

# `python scripts/civic_calibration_report.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import civic_membership as cm  # noqa: E402
from app.tasks import civic_promotion as cp  # noqa: E402

_DEFAULT_QS = (0, 25, 50, 75, 90, 100)

#: 三个可调门槛的旋钮名（归因与扫描都按这个顺序输出，保证可 diff）
GATE_WORLD_DAYS = "min_world_days"
GATE_PEERS = "min_peers"
GATE_FAMILIARITY = "min_familiarity"
TUNABLE_GATES = (GATE_WORLD_DAYS, GATE_PEERS, GATE_FAMILIARITY)

#: 扫描网格每一维的候选值上限。网格是 O(候选数³ × 边数)，生产库上边数上万，
#: 不封顶会把一次「只读报告」拖成分钟级。
_MAX_CANDIDATES_PER_AXIS = 6


def percentiles(values, qs=_DEFAULT_QS) -> dict:
    """最近秩（nearest-rank）分位数。样本为空返回 ``{}``（不是 0 —— 空读数与
    「读数是 0」是两件事，前者要触发「待生产数据复标」）。"""
    ordered = sorted(values)
    if not ordered:
        return {}
    out = {}
    for q in qs:
        idx = int(round(q / 100.0 * (len(ordered) - 1)))
        out[f"p{q}"] = ordered[min(max(idx, 0), len(ordered) - 1)]
    return out


def _floor_to(value: float, digits: int) -> float:
    """向下取整到 ``digits`` 位小数。

    候选阈值**只能向下取整**：分位数返回的是某位居民的真实读数，向上取整会让
    那位居民过不了由他自己产生的阈值，报告里的 ``size`` 就不再是把这三个数字
    抄进 env 后真会发生的结果。
    """
    scale = 10 ** digits
    return math.floor(value * scale) / scale


def _cap_evenly(values: list, limit: int) -> list:
    """把候选值均匀抽稀到至多 ``limit`` 个（保留首尾）。"""
    if len(values) <= limit:
        return list(values)
    if limit <= 1:
        return [values[0]]
    step = (len(values) - 1) / (limit - 1)
    picked = {values[min(int(round(i * step)), len(values) - 1)]
              for i in range(limit)}
    return sorted(picked)


def _anchored_edges(snap, denizens, anchors) -> dict[str, list[float]]:
    """``{denizen_id: 对锚定公民的 familiarity 降序全表}``。

    一趟扫边（而不是每人扫一遍全表）：生产库上 denizen × edges 是二次的，而扫描
    网格要反复用这张表。语义与 ``promotion_evidence`` 的 ``top_familiarity``
    一致（无向边两个方向都认）。
    """
    acc: dict[str, list[float]] = {f.resident_id: [] for f in denizens}
    for party_a, party_b, fam in snap.familiarity:
        if party_a in acc and party_b in anchors:
            acc[party_a].append(fam)
        if party_b in acc and party_a in anchors:
            acc[party_b].append(fam)
    for edges in acc.values():
        edges.sort(reverse=True)
    return acc


def attribute_gates(snap, denizens, edges_by_id: dict[str, list[float]], *,
                    min_world_days: float, min_peers: int,
                    min_familiarity: float) -> dict:
    """**每道闸各拒了谁**。判据逐字照抄 ``select_promotions`` 的三条 continue。

    为什么必须有这一节：``verdict == "partial"`` 只说明「这一组取值形状对」，
    完全掩盖得住「其中两道闸从来没拒过任何人」——那正是
    ``rep_credit_min_score = -0.3`` 的失效签名（闸门在，拒绝面 0/13）。所以
    ``decorative_gates`` 列出拒绝面为 0 的可调门槛。

    ``peers_breakdown`` 把门槛②的失败再拆一层，因为它是**两个**旋钮共用的一道
    闸：``too_few_anchored_edges``（锚定边总数就不够 k 条，此时**只有** k 救得
    了他，θ 归零也没用）vs ``kth_best_below_theta``（边够多但第 k 高不到 θ，此时
    **降 k 或降 θ 都救得了他**）。拆开是为了看出「该动哪个旋钮更划算」。

    ⚠️ 拆开的是**成因**，不是拒绝面的归属：两个桶里的人都是被门槛② 拒的，所以
    ``rejected_by["peers"]`` 是两桶之和，``min_peers`` 是否装饰也按这个和判。
    只按 ``too_few`` 判会在 k>1 时把一道正在拒人的闸报成装饰性。

    ``passed`` 必须等于 ``select_promotions`` 的输出长度——有测试逐场景核对，
    ``agrees_with_select_promotions`` 是运行期的同一条核对。
    """
    passed = banned = 0
    days_only = peers_only = both = 0
    rej_days = too_few = kth_below = 0
    for fact in denizens:
        if fact.banned:
            banned += 1
            continue
        age = (snap.now_world - fact.anchor_world) / timedelta(days=1)
        fails_days = age < min_world_days
        edges = edges_by_id.get(fact.resident_id, [])
        qualified = sum(1 for fam in edges if fam >= min_familiarity)
        fails_peers = qualified < min_peers
        if fails_days:
            rej_days += 1
        if fails_peers:
            if len(edges) < min_peers:
                too_few += 1
            else:
                kth_below += 1
        if fails_days and fails_peers:
            both += 1
        elif fails_days:
            days_only += 1
        elif fails_peers:
            peers_only += 1
        else:
            passed += 1
    peers_rejected = too_few + kth_below
    decorative = []
    if rej_days == 0:
        decorative.append(GATE_WORLD_DAYS)
    if peers_rejected == 0:
        # 门槛②的拒绝条件是 `qualified < k`。判据必须是**整道闸的**拒绝面，不能
        # 只看 too_few：「边够多但达标边不够」的人（记在 kth_below 里）同样是被
        # k 拒的——把 k 降到他的 qualified 就能救他。只看 too_few 会在
        # k>1 时把一道正在拒人的闸报成装饰性，运维照着挑不出该抄哪一行。
        decorative.append(GATE_PEERS)
    if kth_below == 0:
        # θ 的独立贡献：边够多（len ≥ k）却因为达标线被拒。为 0 说明把 θ 归零
        # 也不会多放行一个人。
        decorative.append(GATE_FAMILIARITY)
    return {
        "passed": passed,
        "rejected_by": {"world_days": rej_days,
                        "peers": peers_rejected,
                        "banned": banned},
        "blocked": {"world_days_only": days_only, "peers_only": peers_only,
                    "both": both},
        "peers_breakdown": {"too_few_anchored_edges": too_few,
                            "kth_best_below_theta": kth_below},
        "decorative_gates": decorative,
    }


def _verdict(size: int, total: int) -> str:
    if not total:
        return "no_data"
    if size == 0:
        return "empty"
    return "full" if size == total else "partial"


def numeric_gates(citizen_count: int) -> dict:
    """候选面**之后**的两道数值闸（``civic_membership.py:398-421`` 的旋钮）。

    - ``max_per_run``：单夜上限，超出按确定性顺序**截断**，余量下夜再来；
    - ``breaker_threshold`` = ``max(breaker_min_abs, 公民数 × breaker_fraction)``，
      候选集**大于**它 → **整批拒绝且不截断**（截断会掩盖「阈值写反」这类全量误判）。

    为什么标定报告要管它：晋升面大小 ``size`` 不等于当晚真会放行的人数。一组
    ``size`` 远超熔断线的取值抄进 env，开闸当晚是**晋升 0 人**，而报告只说
    「非空且非全量」——形状对、结果空。这两个旋钮当前还没有消费点（promotion
    pass 尚未接进 ``nightly_cron``），所以这里给的是**开闸预告**：数值由旋钮函数
    现读，组合规则照抄上述 docstring，真接线时两边必须一起看。
    """
    return {
        "max_per_run": cm.promotion_max_per_run(),
        "breaker_threshold": float(max(
            cm.promotion_breaker_min_abs(),
            citizen_count * cm.promotion_breaker_fraction())),
        "breaker_fraction": cm.promotion_breaker_fraction(),
        "breaker_min_abs": cm.promotion_breaker_min_abs(),
    }


def sweep_thresholds(snap, denizens, edges_by_id, *, seasoning: float,
                     days_candidates: list[float], peers_candidates: list[int],
                     theta_candidates: list[float], gates: dict) -> list[dict]:
    """网格扫描：每一组候选阈值下的晋升面，用 ``select_promotions`` 本尊算。

    候选值由调用方从**实测分布**推出（见 :func:`collect_calibration`）——本函数
    不认识任何常数，空库进来就是空网格出去。
    """
    total = len(denizens)
    grid: list[dict] = []
    for days in days_candidates:
        for peers in peers_candidates:
            for theta in theta_candidates:
                picked = cp.select_promotions(
                    snap, min_world_days=days, min_peers=peers,
                    min_familiarity=theta, seasoning_days=seasoning)
                attr = attribute_gates(
                    snap, denizens, edges_by_id, min_world_days=days,
                    min_peers=peers, min_familiarity=theta)
                size = len(picked)
                grid.append({
                    GATE_WORLD_DAYS: days,
                    GATE_PEERS: peers,
                    GATE_FAMILIARITY: theta,
                    "size": size,
                    "verdict": _verdict(size, total),
                    "decorative_gates": attr["decorative_gates"],
                    "exceeds_max_per_run": size > gates["max_per_run"],
                    "trips_breaker": size > gates["breaker_threshold"],
                })
    return grid


async def collect_calibration(db, *, top_n: int = 5) -> dict:
    """三张分布表 + 候选面判据 + 门槛归因 + 实测扫描。**只读**。"""
    seasoning = cm.peer_seasoning_world_days()
    theta = cm.min_familiarity()
    k = max(1, cm.min_peers())
    days_threshold = cm.min_world_days()

    snap = await cp.build_snapshot(db)
    anchors = cp.anchored_citizen_ids(snap, seasoning_days=seasoning)

    citizens = [f for f in snap.facts if f.resident_type in cm.CIVIC_VOTER_TYPES]
    denizens = [f for f in snap.facts
                if f.is_ugc and f.resident_type == cm.UGC_RESIDENT_TYPE]

    # 表①：UGC 的在镇世界日（锚在公民时钟上，与 build_snapshot 同源）
    world_days_exact = [(snap.now_world - f.anchor_world) / timedelta(days=1)
                        for f in denizens]
    world_days = [round(v, 2) for v in world_days_exact]   # 展示用
    # 扫描候选值必须用**未取整**的读数：round 可能向上，把产生该候选值的那位
    # 居民自己挡在门外，报告里的 size 就与照抄 env 后的真实结果不符。

    # 表②：每位 UGC 对锚定公民的 top-N familiarity，以及「第 k 高」那一档
    edges_by_id = _anchored_edges(snap, denizens, anchors)
    per_resident_top = {f.slug: [round(x, 4) for x in edges_by_id[f.resident_id]
                                 [:top_n]] for f in denizens}
    kth = sorted(edges_by_id[f.resident_id][k - 1] for f in denizens
                 if len(edges_by_id[f.resident_id]) >= k)

    # 候选面：用与夜间任务**同一个**判定函数
    candidate_ids = cp.select_promotions(
        snap, min_world_days=days_threshold, min_peers=k,
        min_familiarity=theta, seasoning_days=seasoning,
    )
    slug_by_id = {f.resident_id: f.slug for f in snap.facts}
    verdict = _verdict(len(candidate_ids), len(denizens))

    attribution = attribute_gates(
        snap, denizens, edges_by_id, min_world_days=days_threshold,
        min_peers=k, min_familiarity=theta)
    attribution["agrees_with_select_promotions"] = (
        attribution["passed"] == len(candidate_ids))

    # 扫描候选值：**全部来自上面的分布**，向下取整以保证「产生这个候选值的那位
    # 居民自己能过」——报告里的 size 必须是把这三个数字抄进 env 后真会发生的。
    days_candidates = _cap_evenly(
        sorted({_floor_to(v, 2) for v in percentiles(world_days_exact).values()}),
        _MAX_CANDIDATES_PER_AXIS)
    all_anchored = [fam for edges in edges_by_id.values() for fam in edges]
    theta_candidates = _cap_evenly(
        sorted({_floor_to(v, 4) for v in
                (*percentiles(all_anchored).values(), *percentiles(kth).values())}),
        _MAX_CANDIDATES_PER_AXIS)
    max_edges = max((len(e) for e in edges_by_id.values()), default=0)
    peers_candidates = list(range(1, min(max_edges, _MAX_CANDIDATES_PER_AXIS) + 1))
    gates = numeric_gates(len(citizens))
    grid = sweep_thresholds(
        snap, denizens, edges_by_id, seasoning=seasoning,
        days_candidates=days_candidates, peers_candidates=peers_candidates,
        theta_candidates=theta_candidates, gates=gates)
    # 排序：① 越过熔断线的取值垫底（它当晚放行 0 人，比「形状不对」还糟）；
    # ② 再把「三闸皆有拒绝面」的顶到最前——形状对（partial）只是必要条件，一组
    # 让两道闸空转的取值等于开着两个装饰性闸门上线。
    partial = sorted((row for row in grid if row["verdict"] == "partial"),
                     key=lambda r: (r["trips_breaker"],
                                    len(r["decorative_gates"]), r["size"],
                                    r[GATE_WORLD_DAYS], r[GATE_PEERS],
                                    r[GATE_FAMILIARITY]))

    return {
        "world_at": snap.now_world.isoformat(),
        "thresholds": {
            "min_world_days": cm.min_world_days(),
            "min_peers": k,
            "min_familiarity": theta,
            "peer_seasoning_world_days": seasoning,
        },
        "citizens": {
            "total": len(citizens),
            "builtin": sum(1 for f in citizens if f.is_builtin),
            "naturalised": sum(1 for f in citizens if not f.is_builtin),
        },
        "anchors": len(anchors),
        "ugc": {
            "count": len(denizens),
            "world_days": percentiles(world_days),
            "world_days_raw": sorted(world_days),
        },
        "familiarity": {
            "top_n": top_n,
            "per_resident_top": per_resident_top,
            "kth_best": {"k": k, "values": [round(x, 4) for x in kth],
                         "percentiles": percentiles(kth)},
        },
        "candidate_face": {
            "size": len(candidate_ids),
            "total_ugc": len(denizens),
            "slugs": sorted(slug_by_id.get(i, i) for i in candidate_ids),
            "verdict": verdict,
        },
        "gate_attribution": attribution,
        "numeric_gates": gates,
        "sweep": {
            "candidates": {
                GATE_WORLD_DAYS: days_candidates,
                GATE_PEERS: peers_candidates,
                GATE_FAMILIARITY: theta_candidates,
            },
            "grid": grid,
            "partial": partial,
        },
        # spec §4.2 的降级路径：只有「非空且非全量」才算标定出了一组可用取值，
        # 其余一律要求用生产数据复标。
        "needs_production_recalibration": verdict != "partial",
    }


def _fmt_pct(d: dict) -> str:
    return "  ".join(f"{k}={v}" for k, v in d.items()) if d else "（无样本）"


#: 旋钮名 → 报告里的中文说法（只用于渲染）
_GATE_LABEL = {
    GATE_WORLD_DAYS: "MIN_WORLD_DAYS（在镇世界日）",
    GATE_PEERS: "MIN_PEERS（锚定同伴数）",
    GATE_FAMILIARITY: "MIN_FAMILIARITY（θ）",
}


def _render_attribution(data: dict) -> list[str]:
    """当前阈值下逐闸的**拒绝面**。verdict 说形状，这一节说是谁在拒。"""
    ga = data["gate_attribution"]
    total = data["candidate_face"]["total_ugc"]
    rej, blk, pb = ga["rejected_by"], ga["blocked"], ga["peers_breakdown"]
    out = [
        "",
        "-- 门槛归因（当前阈值下每道闸各拒了谁）--",
        f"  通过 {ga['passed']} / {total}；被拒 "
        f"{blk['world_days_only'] + blk['peers_only'] + blk['both']}"
        f"（仅世界日 {blk['world_days_only']} · 仅同伴 {blk['peers_only']} · "
        f"两者皆 {blk['both']}）；civic_ban 排除 {rej['banned']}",
        f"  逐闸拒绝面：世界日 {rej['world_days']}/{total} · "
        f"同伴 {rej['peers']}/{total}"
        f"（其中锚定边不足 k 条 {pb['too_few_anchored_edges']}、"
        f"第 k 高 < θ {pb['kth_best_below_theta']}）",
    ]
    if not ga.get("agrees_with_select_promotions", True):
        out.append(f"  ⚠️ 归因通过数 {ga['passed']} 与 select_promotions 的 "
                   f"{data['candidate_face']['size']} 不一致——归因逻辑已与判定层"
                   "漂移，本节读数不可用。")
    if ga["decorative_gates"] and total:
        names = "、".join(_GATE_LABEL[g] for g in ga["decorative_gates"])
        out.append(f"  ⚠️ **装饰性闸门（拒绝面 0）**：{names}")
        out.append("     它在当前阈值下没有参与任何一次拒绝——这正是 "
                   "rep_credit_min_score = -0.3 的失效签名（闸门在，拒绝面 "
                   "0/13）。verdict=partial 掩盖得住它，所以必须单列。")
    return out


def _render_sweep(data: dict) -> list[str]:
    """实测扫描：直接答出「哪几组取值使晋升面非空且非全量」。"""
    sweep = data["sweep"]
    cand = sweep["candidates"]
    grid, partial = sweep["grid"], sweep["partial"]
    total = data["candidate_face"]["total_ugc"]
    out = [
        "",
        "-- 实测扫描（候选值全部来自上面的分布，报告里没有任何预填常数）--",
        f"  候选值：MIN_WORLD_DAYS={cand[GATE_WORLD_DAYS]} "
        f"MIN_PEERS={cand[GATE_PEERS]} MIN_FAMILIARITY={cand[GATE_FAMILIARITY]}",
        f"  扫描 {len(grid)} 组；其中 {len(partial)} 组使晋升面非空且非全量"
        + (f"（晋升 {min(r['size'] for r in partial)}–"
           f"{max(r['size'] for r in partial)} / {total} 人；"
           f"{sum(1 for r in partial if not r['decorative_gates'])} 组三闸皆有"
           "拒绝面）" if partial else ""),
    ]
    if not partial:
        out.append("  ⚠️ 没有任何一组实测取值能让晋升面非空且非全量——"
                   "这不是「阈值没调好」，是**这批读数标定不出阈值**"
                   "（样本太少 / 锚定边缺失 / 全部落在同一边界情形上）。")
        return out
    gates = data["numeric_gates"]
    out.append("  ✅ 可用取值（照抄任意一行进 env）——注意 **size 是候选面大小**，"
               "不是当晚真会放行的人数：")
    out.append(f"     候选面之后还有两道数值闸：单夜上限 "
               f"MAX_PER_RUN={gates['max_per_run']}（超出**截断**）、熔断线 "
               f"{gates['breaker_threshold']:g}"
               f"（= max({gates['breaker_min_abs']}, 公民数 × "
               f"{gates['breaker_fraction']:g})，候选集大于它 → **整批拒绝、"
               f"不截断**，当晚放行 0 人）。")
    for row in partial[:10]:
        deco = row["decorative_gates"]
        tags = ["三闸皆有拒绝面"] if not deco else ["空转：" + "/".join(deco)]
        if row["trips_breaker"]:
            tags.append("⚠️ 越熔断线→整批拒绝")
        elif row["exceeds_max_per_run"]:
            tags.append("单夜上限截断")
        out.append(
            f"    CIVIC_PROMOTION_MIN_WORLD_DAYS={row[GATE_WORLD_DAYS]:<10}"
            f"MIN_PEERS={row[GATE_PEERS]:<3}"
            f"MIN_FAMILIARITY={row[GATE_FAMILIARITY]:<8}"
            f"→ {row['size']}/{total} 人  [{' · '.join(tags)}]")
    if len(partial) > 10:
        out.append(f"    …… 另有 {len(partial) - 10} 组"
                   "（完整网格在 collect_calibration 返回的 sweep.grid 里）")
    out.append("  注：扫描只保证「形状对」。选行时优先取「三闸皆有拒绝面」且不带"
               "熔断标记的那几行——空转的闸门等于没上线，越熔断线的取值当晚放行 "
               "0 人。")
    return out


def render_calibration(data: dict, *, list_residents: bool = False) -> str:
    t = data["thresholds"]
    c = data["citizens"]
    u = data["ugc"]
    f = data["familiarity"]
    face = data["candidate_face"]
    out = [
        "== F2 门槛标定报告（只读 · 零 LLM）==",
        f"  世界时间 {data['world_at']}",
        f"  当前阈值（占位默认值）：MIN_WORLD_DAYS={t['min_world_days']} "
        f"MIN_PEERS={t['min_peers']} MIN_FAMILIARITY={t['min_familiarity']} "
        f"SEASONING={t['peer_seasoning_world_days']}",
        "",
        "-- 表③ 公民总数（定 CIVIC_MIN_ELECTORATE / 单夜上限 / 熔断阈值）--",
        f"  公民 {c['total']}（内置 {c['builtin']} / 归化 {c['naturalised']}）；"
        f"锚定公民集 {data['anchors']}",
        "",
        "-- 表① UGC 在镇世界日分布（定 MIN_WORLD_DAYS）--",
        f"  样本 {u['count']} 人：{_fmt_pct(u['world_days'])}",
        "  注：内置阵容世界龄 ≈450 世界日、UGC 从 0 起算，两类人不进同一分布，",
        "      所以本表只统计 UGC denizen。",
        "",
        f"-- 表② 对锚定公民的 top-{f['top_n']} familiarity（定 MIN_FAMILIARITY "
        "/ MIN_PEERS）--",
        f"  第 {f['kth_best']['k']} 高那一档（通过门槛② 当且仅当它 ≥ θ）："
        f"{_fmt_pct(f['kth_best']['percentiles'])}",
        f"  达到 {f['kth_best']['k']} 条锚定边的 UGC：{len(f['kth_best']['values'])}"
        f" / {u['count']}",
    ]
    if list_residents:
        for slug, tops in sorted(f["per_resident_top"].items()):
            out.append(f"    {slug:<24} {tops}")
    out.append("")
    out.append("-- 候选面（用夜间任务同一个 select_promotions 算）--")
    out.append(f"  当前阈值下会晋升 {face['size']} / {face['total_ugc']} 人："
               f"{face['slugs'][:20]}")
    verdict_note = {
        "partial": "✅ 非空且非全量 —— 这组取值形状正确",
        "empty": "🔴 晋升面为空：阈值写紧了，或 familiarity 的主增长路径没开"
                 "（先确认生产 REALISM_RELATIONS_ENABLED 的实际取值）",
        "full": "🔴 晋升面是全量：阈值写松了，开闸当晚会整批放行",
        "no_data": "🔴 库里没有 UGC denizen —— 读数为空，标定没有发生",
    }
    out.append("  " + verdict_note[face["verdict"]])
    out.extend(_render_attribution(data))
    out.extend(_render_sweep(data))
    if data["needs_production_recalibration"]:
        out.append("")
        out.append("  ⚠️ **待生产数据复标，不得直接开闸**（spec §4.2 的降级路径："
                   "以本机 dev 库标定必须显式标注）。")
        out.append("     开闸前要补的三件事：① 在有真实 UGC 的库上重跑本报告；"
                   "② 把三个阈值调到 verdict=partial；③ 复验生产 "
                   "REALISM_RELATIONS_ENABLED=true。")
    return "\n".join(out)


async def _run(top_n: int, list_residents: bool) -> str:
    from app.database import async_session, engine

    async with async_session() as db:
        data = await collect_calibration(db, top_n=top_n)
    await engine.dispose()
    return render_calibration(data, list_residents=list_residents)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="F2 三个门槛的只读标定报告（无写入，无 --dry-run）")
    parser.add_argument("--top-n", type=int, default=5,
                        help="每位 UGC 输出多少条最强的锚定边（默认 5）")
    parser.add_argument("--list", action="store_true",
                        help="逐人列出 top-N（默认只给分布）")
    args = parser.parse_args(argv)
    print(asyncio.run(_run(max(args.top_n, 1), args.list)))


if __name__ == "__main__":
    main()
