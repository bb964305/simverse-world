#!/usr/bin/env python3
"""Dry-run/apply reconciliation for replayed preset story arcs.

The template-key migration (057) deliberately preserved historical duplicate
rows so production could be audited first. This script is the follow-up tool:
it reports every replayed preset arc and can, after an exact-count handshake,
mark the later duplicates as ``superseded`` without deleting history or trying
to undo already-fired side effects.

Examples::

    python scripts/reconcile_preset_arcs.py
    python scripts/reconcile_preset_arcs.py --manifest /tmp/preset-arcs.json
    python scripts/reconcile_preset_arcs.py --apply --expect-duplicates 8 \
        --manifest /tmp/preset-arcs.json
    python scripts/reconcile_preset_arcs.py --rollback-manifest /tmp/preset-arcs.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import async_session  # noqa: E402
from app.models.goal_investment import GoalInvestment  # noqa: E402
from app.models.resident import Resident  # noqa: E402
from app.models.resident_goal import ResidentGoal  # noqa: E402
from seed.preset_characters import PRESET_ARCS, preset_arc_template_key  # noqa: E402


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    # SQLite drops timezone metadata even for DateTime(timezone=True). Treat
    # those stored values as UTC, matching the application write convention,
    # rather than letting astimezone() guess from the operator host timezone.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _goal_payload(row: Any) -> dict[str, str | None]:
    return {
        "id": row.id,
        "status": row.status,
        "created_at": _iso(row.created_at),
        "resolved_at": _iso(row.resolved_at),
        "template_key": row.template_key,
    }


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _extract_duplicate_statuses(manifest: dict[str, Any]) -> dict[str, str]:
    if manifest.get("report_type") != "preset_arc_reconciliation":
        raise RuntimeError("rollback manifest is not a preset arc reconciliation report")
    duplicate_statuses: dict[str, str] = {}
    for group in manifest.get("groups", []):
        for duplicate in group.get("duplicates", []):
            goal_id = duplicate.get("id")
            old_status = duplicate.get("status")
            if not isinstance(goal_id, str) or not isinstance(old_status, str):
                raise RuntimeError("rollback manifest is missing duplicate id/status fields")
            duplicate_statuses[goal_id] = old_status
    return duplicate_statuses


async def _build_report(db) -> tuple[dict[str, Any], list[str]]:
    rows = (await db.execute(
        select(
            Resident.slug.label("resident_slug"),
            ResidentGoal.resident_id,
            ResidentGoal.id,
            ResidentGoal.title,
            ResidentGoal.template_key,
            ResidentGoal.status,
            ResidentGoal.created_at,
            ResidentGoal.resolved_at,
        )
        .join(Resident, Resident.id == ResidentGoal.resident_id)
        .where(
            Resident.slug.in_(list(PRESET_ARCS)),
            ResidentGoal.kind == "arc",
        )
        .order_by(Resident.slug.asc(), ResidentGoal.created_at.asc(), ResidentGoal.id.asc())
    )).all()

    by_slug: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        preset = PRESET_ARCS.get(row.resident_slug)
        if preset and row.title == preset["title"]:
            by_slug[row.resident_slug].append(row)

    groups: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    actionable_duplicate_ids: list[str] = []
    already_superseded_count = 0

    for slug, preset in PRESET_ARCS.items():
        matching = by_slug.get(slug, [])
        if not matching:
            continue
        template_key = preset_arc_template_key(slug)
        canonical_rows = [row for row in matching if row.template_key == template_key]
        if len(canonical_rows) != 1:
            anomalies.append({
                "resident_slug": slug,
                "title": preset["title"],
                "expected_template_key": template_key,
                "row_ids": [row.id for row in matching],
                "reason": "missing_or_ambiguous_canonical",
            })
            continue
        canonical = canonical_rows[0]
        earlier_rows = [
            row.id for row in matching
            if row.id != canonical.id
            and (row.created_at, row.id) < (canonical.created_at, canonical.id)
        ]
        if earlier_rows:
            anomalies.append({
                "resident_slug": slug,
                "title": preset["title"],
                "expected_template_key": template_key,
                "row_ids": earlier_rows,
                "reason": "canonical_is_not_earliest_row",
            })
        unexpected_keyed = [
            row.id for row in matching
            if row.id != canonical.id and row.template_key is not None
        ]
        if unexpected_keyed:
            anomalies.append({
                "resident_slug": slug,
                "title": preset["title"],
                "expected_template_key": template_key,
                "row_ids": unexpected_keyed,
                "reason": "duplicate_rows_unexpected_template_key",
            })

        duplicates = [row for row in matching if row.id != canonical.id]
        actionable = [row for row in duplicates if row.status != "superseded"]
        already_superseded = [row.id for row in duplicates if row.status == "superseded"]
        already_superseded_count += len(already_superseded)
        actionable_duplicate_ids.extend(row.id for row in actionable)

        if actionable or already_superseded:
            groups.append({
                "resident_slug": slug,
                "resident_id": canonical.resident_id,
                "title": preset["title"],
                "template_key": template_key,
                "canonical": _goal_payload(canonical),
                "duplicates": [_goal_payload(row) for row in actionable],
                "already_superseded_ids": already_superseded,
            })

    dependency_rows = []
    if actionable_duplicate_ids:
        dependency_rows = (await db.execute(
            select(
                GoalInvestment.goal_id,
                func.count(GoalInvestment.id).label("count"),
            )
            .where(GoalInvestment.goal_id.in_(actionable_duplicate_ids))
            .group_by(GoalInvestment.goal_id)
            .order_by(GoalInvestment.goal_id.asc())
        )).all()

    dependency_counts = [
        {"goal_id": row.goal_id, "count": int(row.count)}
        for row in dependency_rows
    ]
    report = {
        "report_type": "preset_arc_reconciliation",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "dry_run",
        "duplicate_count": len(actionable_duplicate_ids),
        "already_superseded_count": already_superseded_count,
        "goal_investment_dependency_count": sum(item["count"] for item in dependency_counts),
        "goal_investment_dependencies": dependency_counts,
        "unsafe_group_count": len(anomalies),
        "anomalies": anomalies,
        "groups": groups,
    }
    return report, actionable_duplicate_ids


async def _rollback_from_manifest(db, manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    duplicate_statuses = _extract_duplicate_statuses(manifest)
    if not duplicate_statuses:
        result = {
            "report_type": "preset_arc_reconciliation_rollback",
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": "rollback",
            "restored_count": 0,
            "restored_ids": [],
            "manifest_path": str(manifest_path),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return result

    goals = (await db.execute(
        select(ResidentGoal).where(ResidentGoal.id.in_(list(duplicate_statuses)))
    )).scalars().all()
    found_ids = {goal.id for goal in goals}
    missing = sorted(set(duplicate_statuses) - found_ids)
    if missing:
        raise RuntimeError(
            "rollback manifest references missing resident_goals: " + ", ".join(missing)
        )

    restored_ids: list[str] = []
    for goal in goals:
        previous_status = duplicate_statuses[goal.id]
        if goal.status == previous_status:
            continue
        if goal.status != "superseded":
            raise RuntimeError(
                f"rollback manifest expects goal {goal.id} to be superseded, "
                f"found {goal.status}"
            )
        goal.status = previous_status
        restored_ids.append(goal.id)
    if restored_ids:
        await db.commit()

    result = {
        "report_type": "preset_arc_reconciliation_rollback",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "rollback",
        "restored_count": len(restored_ids),
        "restored_ids": sorted(restored_ids),
        "manifest_path": str(manifest_path),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return result


async def main(
    *,
    apply: bool = False,
    expect_duplicates: int | None = None,
    manifest_path: str | Path | None = None,
    rollback_manifest: str | Path | None = None,
) -> dict[str, Any]:
    """Preview by default; mutate only after an exact duplicate-count handshake."""
    manifest_file = Path(manifest_path) if manifest_path is not None else None
    rollback_file = Path(rollback_manifest) if rollback_manifest is not None else None

    if apply and rollback_file is not None:
        raise RuntimeError("--apply cannot be combined with --rollback-manifest")
    if rollback_file is not None and manifest_file is not None:
        raise RuntimeError("--manifest is not used with --rollback-manifest")
    if rollback_file is not None and expect_duplicates is not None:
        raise RuntimeError("--expect-duplicates is not used with --rollback-manifest")
    if not apply and rollback_file is None and expect_duplicates is not None:
        raise RuntimeError("--expect-duplicates requires --apply")

    async with async_session() as db:
        if rollback_file is not None:
            return await _rollback_from_manifest(db, rollback_file)

        report, duplicate_ids = await _build_report(db)
        if not apply:
            if manifest_file is not None:
                _write_manifest(manifest_file, report)
                report["manifest_path"] = str(manifest_file)
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return report

        if expect_duplicates is None:
            raise RuntimeError("--apply requires --expect-duplicates N")
        if expect_duplicates < 0:
            raise RuntimeError("--expect-duplicates must be non-negative")
        if len(duplicate_ids) != expect_duplicates:
            raise RuntimeError(
                f"expected {expect_duplicates} duplicate(s), found {len(duplicate_ids)}; "
                "refusing before the first write"
            )
        if report["unsafe_group_count"]:
            raise RuntimeError(
                "unsafe preset arc groups require review before apply: "
                + json.dumps(report["anomalies"], ensure_ascii=False, sort_keys=True)
            )
        if report["goal_investment_dependency_count"]:
            raise RuntimeError(
                "refusing to supersede duplicate arcs with goal_investments dependencies: "
                + json.dumps(report["goal_investment_dependencies"], ensure_ascii=False, sort_keys=True)
            )

        report["mode"] = "apply"
        report["applied_duplicate_ids"] = sorted(duplicate_ids)
        report["applied_at"] = datetime.now(UTC).isoformat()
        # Persist the recovery information before the first write. If writing
        # the manifest fails, apply aborts with the DB untouched. If the later
        # commit fails, rollback is a conservative no-op because rows have not
        # reached the expected superseded state.
        if manifest_file is not None:
            _write_manifest(manifest_file, report)
            report["manifest_path"] = str(manifest_file)

        if duplicate_ids:
            goals = (await db.execute(
                select(ResidentGoal).where(ResidentGoal.id.in_(duplicate_ids))
            )).scalars().all()
            for goal in goals:
                goal.status = "superseded"
            await db.commit()

        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return report


def cli() -> int:
    parser = argparse.ArgumentParser(
        description="report or supersede replayed preset story arcs",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="mark later duplicate preset arcs as superseded after review",
    )
    parser.add_argument(
        "--expect-duplicates",
        type=int,
        help="exact duplicate count observed in the dry-run report",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="optional JSON report path to write",
    )
    parser.add_argument(
        "--rollback-manifest",
        type=Path,
        help="restore duplicate statuses from a prior apply manifest",
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            apply=args.apply,
            expect_duplicates=args.expect_duplicates,
            manifest_path=args.manifest,
            rollback_manifest=args.rollback_manifest,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
