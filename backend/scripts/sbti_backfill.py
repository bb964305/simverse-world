#!/usr/bin/env python3
"""SBTI backfill for existing production residents (Roadmap #11).

Why: the S0 audit (docs/PROGRESS.md 遗留跟进 (a)) found the 26 live vm212
residents carry a forge-time ``meta_json.sbti.type`` but *sparse*
``dimensions`` — 0/26 had an ``A2`` key, so ``civic_service._npc_choice``'s
``dims.get("A2", "M")`` read every voter as "M" and NPC votes piled onto
option 0. Seed presets were fixed in seed/preset_characters.py (agent-S); this
tool repairs the residents that already exist in a database.

Strategy (mirrors seed/preset_characters.py, just inverted):
- **rule** (default, zero LLM): a resident with a known ``type`` gets its
  missing dims filled from that type's own pattern in
  ``sbti_service.TYPE_PATTERNS`` — the exact reverse of ``match_type()``.
  Forge-computed dim values already present are preserved; only holes are
  filled. ``similarity``/``exact`` are recomputed against the declared type's
  pattern so the block stays internally consistent, and the player-facing
  ``type`` identity never changes.
- **--llm** (optional): residents rule cannot repair (no sbti at all, or a
  special/unknown type with no pattern: HHHH/DRUNK) are recomputed from their
  persona via ``sbti_service.compute_sbti`` — which meters every attempt into
  ``llm_usage`` (scenario="sbti") — and the run stops early when the budget
  circuit breaker reports PLAYER_ONLY (global daily budget exhausted).

Safety: **dry-run by default** — prints the per-resident diff and writes
nothing. Pass ``--apply`` to write. Idempotent: complete residents are always
skipped, so re-running converges to all ``skip_complete``.

Usage::

    # 差异报告（默认 dry-run，不写库）
    python scripts/sbti_backfill.py
    # 只看一个人
    python scripts/sbti_backfill.py --slug isabella
    # 真跑（纯规则）
    python scripts/sbti_backfill.py --apply
    # 规则修不了的（缺 sbti / 特殊 type）用 LLM 从 persona 重算
    python scripts/sbti_backfill.py --apply --llm
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# `python scripts/sbti_backfill.py` 直接跑时保证 `app` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.llm.budget import BudgetTier, background_tier  # noqa: E402
from app.models.resident import Resident  # noqa: E402
from app.services.sbti_service import (  # noqa: E402
    DIMENSION_CODES,
    LEVEL_MAP,
    TYPE_PATTERNS,
    _parse_pattern,
    compute_sbti,
    update_meta_with_sbti,
)

_LEVEL_FOR_INT = {v: k for k, v in LEVEL_MAP.items()}  # 1→L 2→M 3→H
_VALID_LEVELS = frozenset(LEVEL_MAP)  # {"L", "M", "H"}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------

def _valid_dims(sbti: dict | None) -> dict[str, str]:
    """The subset of ``dimensions`` that are real L/M/H values."""
    dims = (sbti or {}).get("dimensions") or {}
    return {c: dims[c] for c in DIMENSION_CODES if dims.get(c) in _VALID_LEVELS}


def missing_dims(sbti: dict | None) -> list[str]:
    have = _valid_dims(sbti)
    return [c for c in DIMENSION_CODES if c not in have]


def classify(sbti: dict | None) -> str:
    """'missing' (no usable sbti) | 'partial' (some data) | 'complete' (15 dims)."""
    if not isinstance(sbti, dict) or not sbti:
        return "missing"
    have = _valid_dims(sbti)
    if len(have) == len(DIMENSION_CODES):
        return "complete"
    if sbti.get("type") or have:
        return "partial"
    return "missing"


def rule_fill(sbti: dict | None) -> dict | None:
    """Fill missing dims from the declared type's pattern (zero LLM).

    Returns a full canonical sbti block, or None when rule repair is
    impossible (no ``type``, or a special/unknown type with no pattern).
    """
    type_code = (sbti or {}).get("type")
    info = TYPE_PATTERNS.get(type_code)
    if info is None:
        return None
    pattern_levels = _parse_pattern(info["pattern"])
    existing = _valid_dims(sbti)
    dims = {
        code: existing.get(code, _LEVEL_FOR_INT[level])
        for code, level in zip(DIMENSION_CODES, pattern_levels)
    }
    # Same math as sbti_service.match_type, but scored against the *declared*
    # type so a player's known type never flips under a backfill.
    user_vec = [LEVEL_MAP[dims[c]] for c in DIMENSION_CODES]
    distance = sum(abs(a - b) for a, b in zip(user_vec, pattern_levels))
    exact = sum(1 for a, b in zip(user_vec, pattern_levels) if a == b)
    similarity = max(0, round((1 - distance / 30) * 100))
    return {
        "type": type_code,
        "type_name": info["name"],
        "type_en": info["en"],
        "dimensions": dims,
        "similarity": similarity,
        "exact": exact,
    }


# ---------------------------------------------------------------------------
# Backfill core (session-injected; tests drive it on seeded sqlite)
# ---------------------------------------------------------------------------

async def backfill(
    db,
    *,
    slugs: list[str] | None = None,
    use_llm: bool = False,
    apply: bool = False,
) -> list[dict]:
    """Scan residents and repair incomplete sbti blocks. Returns the report.

    Report entry actions:
      skip_complete          — already has all 15 dims
      would_fill / filled    — dry-run diff / actually written
      needs_llm              — rule cannot repair and --llm not given
      skip_budget_exhausted  — --llm given but breaker says PLAYER_ONLY
      llm_failed             — compute_sbti returned nothing (left untouched)
    """
    stmt = select(Resident).order_by(Resident.slug)
    if slugs:
        stmt = select(Resident).where(Resident.slug.in_(slugs)).order_by(Resident.slug)
    residents = (await db.execute(stmt)).scalars().all()

    report: list[dict] = []
    budget_exhausted = False
    for r in residents:
        sbti = (r.meta_json or {}).get("sbti")
        state = classify(sbti)
        entry: dict = {
            "slug": r.slug,
            "name": r.name,
            "state": state,
            "before_type": (sbti or {}).get("type") if isinstance(sbti, dict) else None,
            "missing_dims": missing_dims(sbti),
            "action": "none",
            "source": None,
        }
        if state == "complete":
            entry["action"] = "skip_complete"
            report.append(entry)
            continue

        new_sbti = rule_fill(sbti)
        source = "rule" if new_sbti is not None else None

        if new_sbti is None:
            if not use_llm:
                entry["action"] = "needs_llm"
                report.append(entry)
                continue
            # Budget circuit breaker: stop burning once the global daily
            # budget is fully spent (same gate the agent loop honours).
            if not budget_exhausted:
                tier = await background_tier(db)
                budget_exhausted = tier == BudgetTier.PLAYER_ONLY
            if budget_exhausted:
                entry["action"] = "skip_budget_exhausted"
                report.append(entry)
                continue
            new_sbti = await compute_sbti(r.name, r.ability_md, r.persona_md, r.soul_md)
            source = "llm"
            if new_sbti is None:
                entry["action"] = "llm_failed"
                report.append(entry)
                continue

        entry["source"] = source
        entry["after_type"] = new_sbti["type"]
        entry["filled_dims"] = {
            c: new_sbti["dimensions"][c] for c in entry["missing_dims"]
        }
        if apply:
            r.meta_json = update_meta_with_sbti(r.meta_json, new_sbti)
            flag_modified(r, "meta_json")
            entry["action"] = "filled"
        else:
            entry["action"] = "would_fill"
        report.append(entry)

    if apply:
        await db.commit()
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def render(report: list[dict], *, apply: bool) -> str:
    lines = [f"SBTI backfill — {'APPLY(已写库)' if apply else 'DRY-RUN(未写库)'}"]
    counts: dict[str, int] = {}
    for e in report:
        counts[e["action"]] = counts.get(e["action"], 0) + 1
        if e["action"] == "skip_complete":
            continue
        detail = ""
        if e["action"] in ("filled", "would_fill"):
            filled = ",".join(f"{k}={v}" for k, v in e["filled_dims"].items())
            detail = (f" type={e.get('after_type')} source={e['source']}"
                      f" 补 {len(e['filled_dims'])} 维: {filled}")
        lines.append(
            f"  [{e['action']:>22}] {e['slug']:<24} state={e['state']}"
            f" before_type={e['before_type']}{detail}"
        )
    lines.append("汇总: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if counts.get("needs_llm"):
        lines.append("提示: needs_llm 的居民缺可反推的 type，加 --llm 从 persona 重算。")
    return "\n".join(lines)


async def _run(args) -> None:
    from app.database import async_session

    async with async_session() as db:
        report = await backfill(
            db, slugs=args.slug, use_llm=args.llm, apply=args.apply
        )
    print(render(report, apply=args.apply))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Backfill 15-dim SBTI dimensions for existing residents "
                    "(Roadmap #11). 默认 dry-run 只出差异报告。")
    parser.add_argument("--apply", action="store_true",
                        help="真正写库（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true",
                        help="显式 dry-run（默认行为，便于脚本自描述）")
    parser.add_argument("--llm", action="store_true",
                        help="规则修不了的居民用 LLM 从 persona 重算"
                             "（计入 llm_usage，预算耗尽自动停）")
    parser.add_argument("--slug", action="append",
                        help="只处理指定 slug（可重复）")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply 与 --dry-run 互斥")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
