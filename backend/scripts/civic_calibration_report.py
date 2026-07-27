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
import os
import sys
from datetime import timedelta

# `python scripts/civic_calibration_report.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import civic_membership as cm  # noqa: E402
from app.tasks import civic_promotion as cp  # noqa: E402

_DEFAULT_QS = (0, 25, 50, 75, 90, 100)


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


async def collect_calibration(db, *, top_n: int = 5) -> dict:
    """三张分布表 + 候选面判据。**只读**。"""
    seasoning = cm.peer_seasoning_world_days()
    theta = cm.min_familiarity()
    k = max(1, cm.min_peers())

    snap = await cp.build_snapshot(db)
    anchors = cp.anchored_citizen_ids(snap, seasoning_days=seasoning)

    citizens = [f for f in snap.facts if f.resident_type in cm.CIVIC_VOTER_TYPES]
    denizens = [f for f in snap.facts
                if f.is_ugc and f.resident_type == cm.UGC_RESIDENT_TYPE]

    # 表①：UGC 的在镇世界日（锚在公民时钟上，与 build_snapshot 同源）
    world_days = [round((snap.now_world - f.anchor_world) / timedelta(days=1), 2)
                  for f in denizens]

    # 表②：每位 UGC 对锚定公民的 top-N familiarity，以及「第 k 高」那一档
    per_resident_top: dict[str, list[float]] = {}
    for fact in denizens:
        edges = sorted(
            (fam for a, b, fam in snap.familiarity
             if (a == fact.resident_id and b in anchors)
             or (b == fact.resident_id and a in anchors)),
            reverse=True,
        )[:top_n]
        per_resident_top[fact.slug] = [round(x, 4) for x in edges]
    kth = sorted(v[k - 1] for v in per_resident_top.values() if len(v) >= k)

    # 候选面：用与夜间任务**同一个**判定函数
    candidate_ids = cp.select_promotions(
        snap, min_world_days=cm.min_world_days(), min_peers=k,
        min_familiarity=theta, seasoning_days=seasoning,
    )
    slug_by_id = {f.resident_id: f.slug for f in snap.facts}
    if not denizens:
        verdict = "no_data"
    elif not candidate_ids:
        verdict = "empty"
    elif len(candidate_ids) == len(denizens):
        verdict = "full"
    else:
        verdict = "partial"

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
        # spec §4.2 的降级路径：只有「非空且非全量」才算标定出了一组可用取值，
        # 其余一律要求用生产数据复标。
        "needs_production_recalibration": verdict != "partial",
    }


def _fmt_pct(d: dict) -> str:
    return "  ".join(f"{k}={v}" for k, v in d.items()) if d else "（无样本）"


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
