#!/usr/bin/env python3
"""REP_* 信用阈值标定（纯只读，零写入）。

用法（vm212 api 容器内跑，DATABASE_URL 由 deploy compose 注入）::

    docker compose exec api python scripts/rep_calibrate.py --reject-fraction 0.15

本地 / 沙盒（任意 DATABASE_URL）::

    DATABASE_URL=sqlite+aiosqlite:////tmp/f1-calib.db DEBUG=true LLM_API_KEY=x \\
        python scripts/rep_calibrate.py

口径注意：

- 分数走 ``reputation_service.project(db, force=True)``——与夜间 ``recompute``
  共用 ``_score_all``，因此标定值和实际写入的值不可能漂移；``force`` 使
  ``REP_ENABLED=false`` 的生产库在开闸前也能被读出真实分布。
- 本脚本**不写库**：没有 commit，没有 UPDATE。
- ``project`` 算的是**一步 EMA**（从库里现存的 ``previous`` 出发）。开闸前
  ``previous`` 恒为 0，读数因此是稳态值的 ``rep_ema_alpha`` 倍；要看稳态，配合
  ``REP_GOSSIP_BASE_TONE`` 等 env 覆盖多跑几晚 ``recompute`` 后再读。
- **分数分布不能单独回答"机制生效了没有"**：``reputation_service._score_all``
  内部逐条 gossip 记忆都会查一次 (holder, subject) 的关系 affinity 来算语气，
  但这个 affinity 只是局部变量，算完语气就丢——从未进入 ``ScoreRow``。如果
  生产里绝大多数 gossip 记忆的 pair 都查不到非零 affinity，最终population的
  分数分布依然会是一条看起来正常的负偏曲线——和"机制生效但大家确实互相
  说坏话"长得一模一样。本脚本因此**自己重跑一次** pair→affinity 的查表
  （``_gossip_affinities``，只读，与 ``_score_all`` 内部逻辑同源），把
  ``describe_affinity_coverage()`` 的读数和分数分布一起端出来，才能回答
  「多大比例的 gossip 落在 ``gossip_tone(0)`` fallback 上」这个问题。
- 退出码：0=可标定且拒绝面非空；2=样本不足/分布退化；3=建议阈值拒绝面为空。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence

# `python scripts/rep_calibrate.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.memory import Memory  # noqa: E402
from app.services.relation_service import canonical_pair  # noqa: E402
from app.services.reputation_service import (  # noqa: E402
    CalibrationError,
    ScoreRow,
    _affinity_lookup,
    describe,
    describe_affinity_coverage,
    project,
    recommend_credit_min_score,
)

TOP_N = 5


def build_report(
    rows: list[ScoreRow],
    reject_fraction: float,
    current_threshold: float,
    affinities: Sequence[float] | None = None,
) -> dict:
    """把一组投影结果整理成可打印/可 JSON 的读数（纯函数，供测试直接调用）。

    ``affinities`` 是可选的——传入时（真实跑法必传，见 ``_run``）报告里会
    追加 ``affinity_coverage`` 一节；不传时（brief 原样的 3 参调用）行为与
    未扩展前逐字节一致，向后兼容。
    """
    scores = [row.score for row in rows]
    by_score = sorted(rows, key=lambda row: row.score)
    report: dict = {
        "n": len(scores),
        "distribution": describe(scores),
        "current_threshold": float(current_threshold),
        "current_rejected": sum(1 for s in scores if s < current_threshold),
        "reject_fraction": float(reject_fraction),
        "lowest": [
            {"slug": row.slug, "score": row.score, "samples": row.samples}
            for row in by_score[:TOP_N]
        ],
        "highest": [
            {"slug": row.slug, "score": row.score, "samples": row.samples}
            for row in by_score[::-1][:TOP_N]
        ],
    }
    if affinities is not None:
        report["affinity_coverage"] = describe_affinity_coverage(affinities)
    try:
        threshold = recommend_credit_min_score(scores, reject_fraction)
    except CalibrationError as exc:
        report["recommended"] = None
        report["recommended_rejected"] = 0
        report["error"] = str(exc)
        return report
    report["recommended"] = threshold
    report["recommended_rejected"] = sum(1 for s in scores if s < threshold)
    return report


def render(report: dict) -> str:
    d = report["distribution"]
    lines = [
        "== REP 信用阈值标定（只读）==",
        f"样本 n={report['n']}  min={d['min']:+.4f}  p10={d['p10']:+.4f}  "
        f"p25={d['p25']:+.4f}  median={d['median']:+.4f}  p75={d['p75']:+.4f}  "
        f"p90={d['p90']:+.4f}  max={d['max']:+.4f}  mean={d['mean']:+.4f}",
        f"负分占比 {d['negative_share'] * 100:.1f}%",
        f"当前 REP_CREDIT_MIN_SCORE={report['current_threshold']:+.4f} → 拒绝 "
        f"{report['current_rejected']}/{report['n']} 人"
        + ("  ← 装饰性闸门（拒绝面为空）" if report["current_rejected"] == 0 else ""),
    ]
    if "affinity_coverage" in report:
        cov = report["affinity_coverage"]
        if cov["n"] == 0:
            lines.append("gossip affinity 覆盖率：无 gossip 样本（n=0），无法判断是否落在 fallback 上")
        else:
            lines.append(
                f"gossip affinity 覆盖率：{cov['covered']}/{cov['n']} "
                f"({cov['coverage_share'] * 100:.1f}%) 命中非零 affinity；"
                f"其余 {cov['uncovered']}/{cov['n']} 条落在 gossip_tone(0) fallback"
                + ("  ← 机制基本没生效，population 的负偏几乎全靠 fallback 常数撑"
                   if cov["coverage_share"] < 0.05 else "")
            )
    if report.get("recommended") is None:
        lines.append(f"建议值：无法标定 — {report.get('error', '样本为空')}")
    else:
        lines.append(
            f"建议 REP_CREDIT_MIN_SCORE={report['recommended']:+.4f}"
            f"（目标拒绝面 {report['reject_fraction'] * 100:.0f}%）→ 拒绝 "
            f"{report['recommended_rejected']}/{report['n']} 人"
        )
    if report["lowest"]:
        lines.append("最低 %d 人: " % len(report["lowest"]) + ", ".join(
            f"{e['slug']}={e['score']:+.4f}(n={e['samples']})" for e in report["lowest"]
        ))
        lines.append("最高 %d 人: " % len(report["highest"]) + ", ".join(
            f"{e['slug']}={e['score']:+.4f}(n={e['samples']})" for e in report["highest"]
        ))
    return "\n".join(lines)


async def _gossip_affinities(db: AsyncSession, resident_ids: list[str]) -> list[float]:
    """逐条 gossip ``Memory`` 的 (holder, subject) affinity——``_score_all``
    内部算完语气就丢的那份（Task 7 交接的缺口，见模块 docstring）。

    刻意与 ``reputation_service._score_all`` 里 pair 收集 / ``_affinity_lookup``
    查表那段重复：``ScoreRow`` 目前只存最终分数，不带来源 affinity，想回答
    "多少 gossip 落在 fallback（affinity==0）上"只能在这里独立重建一次——只读，
    不改变 ``_score_all`` 本身的语义。若未来要去重，可以把这段提炼成
    ``reputation_service`` 里的共享 helper；本任务范围内不做（YAGNI，且会牵动
    已交付并通过审查的 recompute/project 路径）。
    """
    if not resident_ids:
        return []
    memories = (await db.execute(
        select(Memory).where(
            Memory.source == "gossip",
            Memory.related_resident_id.in_(resident_ids),
            Memory.archived_at.is_(None),
        )
    )).scalars().all()

    pairs: set[tuple[str, str]] = set()
    for memory in memories:
        if memory.resident_id and memory.related_resident_id:
            party_a, _, party_b, _ = canonical_pair(
                memory.resident_id, memory.related_resident_id
            )
            pairs.add((party_a, party_b))
    affinity_by_pair = await _affinity_lookup(db, pairs)

    affinities: list[float] = []
    for memory in memories:
        if not memory.resident_id or not memory.related_resident_id:
            continue
        party_a, _, party_b, _ = canonical_pair(
            memory.resident_id, memory.related_resident_id
        )
        affinities.append(affinity_by_pair.get((party_a, party_b), 0.0))
    return affinities


async def _run(reject_fraction: float, db: AsyncSession | None = None) -> dict:
    if db is not None:
        rows = await project(db, force=True)
        affinities = await _gossip_affinities(db, [row.resident_id for row in rows])
        return build_report(rows, reject_fraction, settings.rep_credit_min_score, affinities)

    from app.database import async_session

    async with async_session() as session:
        rows = await project(session, force=True)
        affinities = await _gossip_affinities(session, [row.resident_id for row in rows])
        return build_report(rows, reject_fraction, settings.rep_credit_min_score, affinities)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="REP 信用阈值标定（只读）")
    parser.add_argument("--reject-fraction", type=float, default=0.15,
                        help="目标拒绝面占比（默认 0.15）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    args = parser.parse_args(argv)
    report = asyncio.run(_run(args.reject_fraction))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    if report.get("recommended") is None:
        return 2
    return 0 if report["recommended_rejected"] > 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
