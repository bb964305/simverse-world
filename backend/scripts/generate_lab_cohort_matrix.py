#!/usr/bin/env python3
"""Generate the complete Lab v1-to-v2 financial cutover cohort matrix.

The matrix is deliberately data, not ordered business logic.  Every canonical
Task/Hold/Run/Artifact tuple is evaluated against every positive rule.  A tuple
with no positive match receives the explicit freeze decision; multiple matches
are an error.  Unknown raw values are never coerced into a canonical bucket.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


TASK_STATUSES = (
    "draft",
    "funded",
    "assigned",
    "running",
    "review",
    "rejected",
    "completed",
    "failed",
    "expired",
    "cancelled",
)
HOLD_STATUSES = ("none", "held", "settled", "refunded")
RUN_STATUSES = (
    "none",
    "queued",
    "running",
    "needs_approval",
    "succeeded",
    "failed",
    "cancelled",
)
ARTIFACT_STATES = (
    "none",
    "v1_candidate",
    "v1_verified",
    "v1_rejected_or_quarantined",
)

EXPECTED_MATRIX_SIZE = 10 * 4 * 7 * 4
REQUIRED_MULTIPLICITY_FIELDS = (
    "task_count",
    "hold_count",
    "run_count",
    "artifact_count",
)
ARTIFACT_SCAN_STATUSES = {"skipped", "pending", "clean", "flagged"}
ARTIFACT_VERIFICATION_STATUSES = {"unverified", "verified", "rejected"}


class CohortError(ValueError):
    """Raised when a cohort cannot be classified without guessing."""


@dataclass(frozen=True, order=True)
class Cohort:
    task_status: str
    hold_status: str
    run_status: str
    artifact_state: str


@dataclass(frozen=True)
class Decision:
    rule_id: str
    action: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    action: str
    predicate: Callable[[Cohort], bool]


@dataclass(frozen=True)
class ActualRowCollection:
    database: str
    database_url: str
    disposable: bool
    read_only: bool
    rows: list[dict[str, object]]


POSITIVE_RULES: tuple[Rule, ...] = (
    Rule(
        "cohort.v1-draft-read-only.v1",
        "audit_only",
        lambda row: row == Cohort("draft", "none", "none", "none"),
    ),
    Rule(
        "cohort.funded-held-convert.v1",
        "convert_hold_to_v2",
        lambda row: row == Cohort("funded", "held", "none", "none"),
    ),
    Rule(
        "cohort.active-run-fence.v1",
        "freeze_until_fenced_then_refund",
        lambda row: (
            row.task_status in {"assigned", "running"}
            and row.hold_status == "held"
            and row.run_status in {"queued", "running", "needs_approval"}
            and row.artifact_state in {"none", "v1_candidate"}
        ),
    ),
    Rule(
        "cohort.review-eligible.v1",
        "convert_hold_to_v2_review",
        lambda row: (
            row.task_status == "review"
            and row.hold_status == "held"
            and row.run_status == "succeeded"
            and row.artifact_state in {"v1_candidate", "v1_verified"}
        ),
    ),
    Rule(
        "cohort.rejected-arbitration.v1",
        "convert_hold_to_v2_arbitration",
        lambda row: (
            row.task_status == "rejected"
            and row.hold_status == "held"
            and row.run_status == "succeeded"
            and row.artifact_state
            in {"v1_candidate", "v1_verified", "v1_rejected_or_quarantined"}
        ),
    ),
    Rule(
        "cohort.completed-v1-audit.v1",
        "audit_only",
        lambda row: row == Cohort("completed", "settled", "succeeded", "v1_verified"),
    ),
    Rule(
        "cohort.refunded-v1-audit.v1",
        "audit_only",
        lambda row: (
            row.task_status in {"failed", "expired", "cancelled"}
            and row.hold_status == "refunded"
            and row.run_status in {"none", "failed", "cancelled"}
            and row.artifact_state in {"none", "v1_rejected_or_quarantined"}
        ),
    ),
)

FREEZE_DECISION = Decision("cohort.freeze-unclassified.v1", "freeze")


def _validate_value(value: str, allowed: Sequence[str], field: str) -> None:
    if value not in allowed:
        raise CohortError(f"unknown {field}: {value!r}")


def validate_cohort(row: Cohort) -> None:
    _validate_value(row.task_status, TASK_STATUSES, "task_status")
    _validate_value(row.hold_status, HOLD_STATUSES, "hold_status")
    _validate_value(row.run_status, RUN_STATUSES, "run_status")
    _validate_value(row.artifact_state, ARTIFACT_STATES, "artifact_state")


def classify_cohort(row: Cohort, rules: Sequence[Rule] = POSITIVE_RULES) -> Decision:
    validate_cohort(row)
    matches = [rule for rule in rules if rule.predicate(row)]
    if len(matches) > 1:
        ids = ", ".join(sorted(rule.rule_id for rule in matches))
        raise CohortError(f"overlapping cohort rules for {row}: {ids}")
    if not matches:
        return FREEZE_DECISION
    match = matches[0]
    return Decision(match.rule_id, match.action)


def iter_canonical_cohorts() -> Iterable[Cohort]:
    for task_status in TASK_STATUSES:
        for hold_status in HOLD_STATUSES:
            for run_status in RUN_STATUSES:
                for artifact_state in ARTIFACT_STATES:
                    yield Cohort(task_status, hold_status, run_status, artifact_state)


def generate_matrix(rules: Sequence[Rule] = POSITIVE_RULES) -> dict[str, object]:
    rows: list[dict[str, str]] = []
    coverage: dict[str, int] = {}
    seen: set[Cohort] = set()
    for cohort in iter_canonical_cohorts():
        if cohort in seen:
            raise CohortError(f"duplicate canonical cohort generated: {cohort}")
        seen.add(cohort)
        decision = classify_cohort(cohort, rules)
        coverage[decision.rule_id] = coverage.get(decision.rule_id, 0) + 1
        rows.append({**asdict(cohort), **asdict(decision)})

    if len(rows) != EXPECTED_MATRIX_SIZE or len(seen) != EXPECTED_MATRIX_SIZE:
        raise CohortError(
            f"matrix cardinality mismatch: rows={len(rows)}, unique={len(seen)}, "
            f"expected={EXPECTED_MATRIX_SIZE}"
        )
    expected_rule_ids = {rule.rule_id for rule in rules} | {FREEZE_DECISION.rule_id}
    missing_rules = expected_rule_ids - coverage.keys()
    if missing_rules:
        raise CohortError(f"cohort rules have no coverage: {sorted(missing_rules)}")
    return {
        "schema": "simverse.lab.cohort-matrix.v1",
        "dimensions": {
            "task_status": list(TASK_STATUSES),
            "hold_status": list(HOLD_STATUSES),
            "run_status": list(RUN_STATUSES),
            "artifact_state": list(ARTIFACT_STATES),
        },
        "row_count": len(rows),
        "unique_tuple_count": len(seen),
        "rule_coverage": dict(sorted(coverage.items())),
        "rows": rows,
    }


def _require_nonnegative_int(raw: Mapping[str, object], field: str) -> int:
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CohortError(f"{field} must be a nonnegative integer")
    return value


def _ensure_explicit_multiplicities(raw: Mapping[str, object]) -> dict[str, int]:
    missing = [field for field in REQUIRED_MULTIPLICITY_FIELDS if field not in raw]
    if missing:
        raise CohortError(f"missing multiplicity fields: {', '.join(sorted(missing))}")
    return {
        field: _require_nonnegative_int(raw, field)
        for field in REQUIRED_MULTIPLICITY_FIELDS
    }


def _validate_actual_row_links(
    raw: Mapping[str, object], multiplicity: Mapping[str, int]
) -> None:
    invalid_links: dict[str, dict[str, object]] = {}

    if multiplicity["task_count"] != 1:
        invalid_links["task_count"] = {"actual": multiplicity["task_count"], "expected": 1}

    hold_expected = 0 if raw.get("hold_status") == "none" else 1
    if multiplicity["hold_count"] != hold_expected:
        invalid_links["hold_count"] = {
            "actual": multiplicity["hold_count"],
            "expected": hold_expected,
        }

    run_expected = 0 if raw.get("run_status") == "none" else 1
    if multiplicity["run_count"] != run_expected:
        invalid_links["run_count"] = {
            "actual": multiplicity["run_count"],
            "expected": run_expected,
        }

    artifact_state = raw.get("artifact_state")
    artifact_count = multiplicity["artifact_count"]
    if artifact_state == "none":
        if artifact_count != 0:
            invalid_links["artifact_count"] = {"actual": artifact_count, "expected": 0}
    elif artifact_count != 1:
        invalid_links["artifact_count"] = {"actual": artifact_count, "expected": 1}

    if invalid_links:
        raise CohortError(f"ambiguous actual-row links: {invalid_links}")


def _cohort_value(raw: Mapping[str, object], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str):
        raise CohortError(f"{field} must be a string")
    return value


def _artifact_bucket(raw: Mapping[str, object]) -> str:
    scan_status = raw.get("scan_status")
    verification_status = raw.get("verification_status")
    if scan_status not in ARTIFACT_SCAN_STATUSES:
        raise CohortError(f"unknown artifact scan_status: {scan_status!r}")
    if verification_status not in ARTIFACT_VERIFICATION_STATUSES:
        raise CohortError(
            f"unknown artifact verification_status: {verification_status!r}"
        )
    if scan_status == "flagged" or verification_status == "rejected":
        return "v1_rejected_or_quarantined"
    if scan_status == "clean" and verification_status == "verified":
        return "v1_verified"
    return "v1_candidate"


def canonical_artifact_state(artifacts: Sequence[Mapping[str, object]]) -> str:
    if not artifacts:
        return "none"
    buckets = sorted({_artifact_bucket(row) for row in artifacts})
    if len(buckets) != 1:
        raise CohortError(f"ambiguous artifact states: {buckets}")
    return buckets[0]


def require_nonempty_actual_rows(
    actual_rows: Sequence[Mapping[str, object]], *, source: str
) -> None:
    if not actual_rows:
        raise CohortError(f"actual rows collection is empty for {source}")


def _singleton_or_none(rows: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    return rows[0] if rows else None


def _parse_hold_task_id(reason: object) -> str | None:
    if not isinstance(reason, str) or not reason.startswith("lab_task:"):
        return None
    task_id = reason.removeprefix("lab_task:")
    return task_id or None


def collect_actual_rows_from_snapshot(
    *,
    tasks: Sequence[Mapping[str, object]],
    holds: Sequence[Mapping[str, object]],
    runs: Sequence[Mapping[str, object]],
    artifacts: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Build task-centric raw rows from immutable DB snapshots."""
    tasks_by_id = {
        str(row["id"]): row
        for row in tasks
    }
    holds_by_id = {
        str(row["id"]): row
        for row in holds
    }
    runs_by_id = {
        str(row["id"]): row
        for row in runs
    }
    runs_by_task: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for run in runs:
        run_task_id = run.get("task_id")
        if isinstance(run_task_id, str) and run_task_id:
            runs_by_task[run_task_id].append(run)
    holds_by_task_reason: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for hold in holds:
        task_id = _parse_hold_task_id(hold.get("reason"))
        if task_id is not None:
            holds_by_task_reason[task_id].append(hold)

    artifacts_by_run: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    invalid_artifacts: list[tuple[int, Mapping[str, object], str]] = []
    for index, artifact in enumerate(artifacts):
        run_id = artifact.get("run_id")
        task_id = artifact.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            invalid_artifacts.append(
                (index, artifact, "artifact has no valid task binding")
            )
            continue
        if task_id not in tasks_by_id:
            invalid_artifacts.append(
                (index, artifact, "artifact task_id does not reference a known task")
            )
            continue
        if not isinstance(run_id, str) or not run_id:
            invalid_artifacts.append(
                (index, artifact, "artifact has no valid run binding")
            )
            continue
        run = runs_by_id.get(run_id)
        if run is None:
            invalid_artifacts.append(
                (index, artifact, "artifact run_id does not reference a known run")
            )
            continue
        if run.get("task_id") != task_id:
            invalid_artifacts.append(
                (index, artifact, "artifact task_id does not match its run task_id")
            )
            continue
        artifacts_by_run[run_id].append(artifact)

    rows: list[dict[str, object]] = []
    linked_hold_ids: set[str] = set()
    for task_id, task in sorted(tasks_by_id.items()):
        related_holds: dict[str, Mapping[str, object]] = {}
        hold_id = task.get("hold_id")
        if isinstance(hold_id, str) and hold_id:
            hold = holds_by_id.get(hold_id)
            if hold is not None:
                related_holds[str(hold["id"])] = hold
        for hold in holds_by_task_reason.get(task_id, []):
            related_holds[str(hold["id"])] = hold
        linked_hold_ids.update(related_holds.keys())
        hold_rows = [related_holds[key] for key in sorted(related_holds)]
        hold = _singleton_or_none(hold_rows)

        related_runs = sorted(
            runs_by_task.get(task_id, []), key=lambda row: str(row["id"])
        )
        run = None
        accepted_run_id = task.get("accepted_run_id")
        if isinstance(accepted_run_id, str) and accepted_run_id:
            candidate = runs_by_id.get(accepted_run_id)
            if candidate is not None and candidate.get("task_id") == task_id:
                run = candidate
        elif len(related_runs) == 1:
            run = related_runs[0]
        artifact_rows: list[Mapping[str, object]] = []
        for related_run in related_runs:
            artifact_rows.extend(artifacts_by_run.get(str(related_run["id"]), []))

        artifact_state: object
        collector_error: str | None = None
        try:
            artifact_state = canonical_artifact_state(artifact_rows)
        except CohortError as exc:
            artifact_state = None
            collector_error = str(exc)
        if accepted_run_id and run is None:
            collector_error = "accepted run binding is missing or belongs to another task"
        elif not accepted_run_id and related_runs:
            collector_error = "linked run is not bound by accepted_run_id"

        row: dict[str, object] = {
            "id": f"task:{task_id}",
            "task_id": task_id,
            "hold_id": hold.get("id") if hold is not None else None,
            "run_id": run.get("id") if run is not None else None,
            "task_status": task.get("status"),
            "hold_status": "none" if hold is None else hold.get("status"),
            "run_status": "none" if run is None else run.get("status"),
            "artifact_state": artifact_state,
            "task_count": 1,
            "hold_count": len(hold_rows),
            "run_count": len(related_runs),
            "artifact_count": len(artifact_rows),
        }
        if collector_error is not None:
            row["collector_error"] = collector_error
        rows.append(row)

    for hold_id, hold in sorted(holds_by_id.items()):
        if hold_id in linked_hold_ids:
            continue
        task_id = _parse_hold_task_id(hold.get("reason"))
        row: dict[str, object] = {
            "id": f"hold:{hold_id}",
            "task_id": task_id,
            "hold_id": hold_id,
            "run_id": None,
            "task_status": None,
            "hold_status": hold.get("status"),
            "run_status": "none",
            "artifact_state": "none",
            "task_count": 0,
            "hold_count": 1,
            "run_count": 0,
            "artifact_count": 0,
        }
        rows.append(row)

    for index, artifact, collector_error in invalid_artifacts:
        artifact_id = artifact.get("id")
        identity = str(artifact_id) if artifact_id is not None else f"row-{index}"
        task_id = artifact.get("task_id")
        run_id = artifact.get("run_id")
        rows.append(
            {
                "id": f"artifact:{identity}",
                "artifact_id": artifact_id,
                "task_id": task_id,
                "hold_id": None,
                "run_id": run_id,
                "task_status": None,
                "hold_status": "none",
                "run_status": "none",
                "artifact_state": None,
                "task_count": int(isinstance(task_id, str) and task_id in tasks_by_id),
                "hold_count": 0,
                "run_count": int(isinstance(run_id, str) and run_id in runs_by_id),
                "artifact_count": 1,
                "collector_error": collector_error,
            }
        )

    return rows


def map_actual_rows(
    actual_rows: Sequence[dict[str, object]], rules: Sequence[Rule] = POSITIVE_RULES
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Classify real rows while preserving every fail-closed anomaly.

    Invalid/ambiguous rows are explicitly frozen.  The returned anomaly list is
    non-empty so the CLI exits unsuccessfully; no caller can mistake a frozen
    unknown row for an approved conversion.
    """
    mapped: list[dict[str, object]] = []
    anomalies: list[dict[str, object]] = []
    for index, raw in enumerate(actual_rows):
        identity = raw.get("id", index)
        try:
            collector_error = raw.get("collector_error")
            if collector_error is not None:
                raise CohortError(str(collector_error))
            multiplicity = _ensure_explicit_multiplicities(raw)
            _validate_actual_row_links(raw, multiplicity)
            cohort = Cohort(
                task_status=_cohort_value(raw, "task_status"),
                hold_status=_cohort_value(raw, "hold_status"),
                run_status=_cohort_value(raw, "run_status"),
                artifact_state=_cohort_value(raw, "artifact_state"),
            )
            decision = classify_cohort(cohort, rules)
            payload = {"id": identity, **asdict(cohort), **asdict(decision)}
            if decision == FREEZE_DECISION:
                error = f"unresolved freeze cohort: {decision.rule_id}"
                anomalies.append({"id": identity, "error": error, "raw": raw})
                mapped.append({**payload, "error": error, "unresolved": True})
                continue
            mapped.append(payload)
        except (KeyError, CohortError) as exc:
            anomaly = {"id": identity, "error": str(exc), "raw": raw}
            anomalies.append(anomaly)
            mapped.append(
                {
                    "id": identity,
                    "rule_id": "cohort.freeze-invalid-raw.v1",
                    "action": "freeze",
                    "error": str(exc),
                }
            )
    return mapped, anomalies


async def collect_actual_rows(
    database_url: str, *, assert_disposable: bool
) -> ActualRowCollection:
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+asyncpg":
        raise ValueError("actual-row collection requires a postgresql+asyncpg database URL")

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                database, disposable = (
                    await connection.execute(
                        text(
                            "SELECT current_database(), "
                            "current_setting('simverse.release_disposable', true)"
                        )
                    )
                ).one()
                if assert_disposable and disposable != "on":
                    raise ValueError(
                        "--assert-disposable requires simverse.release_disposable=on"
                    )
                required_tables = (
                    "lab_tasks",
                    "coin_holds",
                    "lab_runs",
                    "lab_artifacts",
                )
                presence = {
                    name: (
                        await connection.execute(
                            text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}
                        )
                    ).scalar_one()
                    for name in required_tables
                }
                missing = sorted(name for name, exists in presence.items() if not exists)
                if missing:
                    raise ValueError(
                        "required cohort tables are missing: " + ", ".join(missing)
                    )
                tasks = list(
                    (
                        await connection.execute(
                            text(
                                "SELECT id, status, hold_id, accepted_run_id "
                                "FROM lab_tasks ORDER BY id"
                            )
                        )
                    ).mappings()
                )
                holds = list(
                    (
                        await connection.execute(
                            text(
                                "SELECT id, status, reason "
                                "FROM coin_holds "
                                "WHERE reason LIKE 'lab_task:%' "
                                "ORDER BY id"
                            )
                        )
                    ).mappings()
                )
                runs = list(
                    (
                        await connection.execute(
                            text(
                                "SELECT id, task_id, status "
                                "FROM lab_runs ORDER BY id"
                            )
                        )
                    ).mappings()
                )
                artifacts = list(
                    (
                        await connection.execute(
                            text(
                                "SELECT id, task_id, run_id, scan_status, verification_status "
                                "FROM lab_artifacts ORDER BY id"
                            )
                        )
                    ).mappings()
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()

    return ActualRowCollection(
        database=str(database),
        database_url=parsed.render_as_string(hide_password=True),
        disposable=disposable == "on",
        read_only=True,
        rows=collect_actual_rows_from_snapshot(
            tasks=tasks,
            holds=holds,
            runs=runs,
            artifacts=artifacts,
        ),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write canonical JSON to this path")
    parser.add_argument(
        "--actual-rows",
        type=Path,
        help="optional JSON array of actual cohort rows to classify fail-closed",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="optional postgresql+asyncpg URL used to collect actual cohort rows read-only",
    )
    parser.add_argument("--assert-disposable", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        database_url = args.database_url
        if args.actual_rows and database_url:
            raise CohortError("use either --actual-rows or --database-url, not both")
        if not args.actual_rows and database_url is None:
            database_url = os.environ.get("LAB_TEST_DATABASE_URL") or os.environ.get(
                "DATABASE_URL"
            )
        document = generate_matrix()
        if args.actual_rows:
            raw = json.loads(args.actual_rows.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise CohortError("--actual-rows must contain a JSON array")
            require_nonempty_actual_rows(raw, source=f"json:{args.actual_rows}")
            mapped, anomalies = map_actual_rows(raw)
            document["actual_row_source"] = {
                "type": "json",
                "path": str(args.actual_rows),
            }
            document["actual_row_mapping"] = mapped
            document["actual_row_anomalies"] = anomalies
        elif database_url:
            collection = asyncio.run(
                collect_actual_rows(
                    database_url,
                    assert_disposable=args.assert_disposable,
                )
            )
            require_nonempty_actual_rows(
                collection.rows, source=f"database:{collection.database}"
            )
            mapped, anomalies = map_actual_rows(collection.rows)
            document["actual_row_source"] = {
                "type": "database",
                "database": collection.database,
                "database_url": collection.database_url,
                "disposable": collection.disposable,
                "read_only": collection.read_only,
            }
            document["actual_row_collection"] = collection.rows
            document["actual_row_mapping"] = mapped
            document["actual_row_anomalies"] = anomalies
        else:
            anomalies = []
        if "actual_row_mapping" in document:
            document["actual_row_summary"] = {
                "row_count": len(document["actual_row_mapping"]),
                "anomaly_count": len(document["actual_row_anomalies"]),
                "unresolved_count": sum(
                    1
                    for row in document["actual_row_mapping"]
                    if row.get("unresolved") is True
                ),
            }
    except (CohortError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2

    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 1 if anomalies else 0


if __name__ == "__main__":
    raise SystemExit(main())
