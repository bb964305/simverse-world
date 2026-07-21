#!/usr/bin/env python3
"""Report Lab escrow inconsistencies without modifying financial state."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


TERMINAL_TASK_STATUSES = {"completed", "failed", "expired", "cancelled"}
REFUND_TASK_STATUSES = {"failed", "expired", "cancelled"}
_TERMINAL_REASON_PREFIXES = (
    "lab_reward:",
    "lab_treasury:",
    "lab_fee:",
    "lab_accept:",
    "lab_auto_release:",
    "lab_arbitrate_settle:",
    "lab_cancel:",
    "lab_fail:",
    "lab_expire:",
    "lab_arbitrate_refund:",
)


def _anomaly(kind: str, **details: object) -> dict[str, object]:
    return {"kind": kind, **details}


def _hold_task_id(reason: object) -> str | None:
    if not isinstance(reason, str) or not reason.startswith("lab_task:"):
        return None
    task_id = reason.removeprefix("lab_task:")
    return task_id or None


def _task_id_from_terminal_reason(reason: object) -> str | None:
    if not isinstance(reason, str):
        return None
    for prefix in _TERMINAL_REASON_PREFIXES:
        if reason.startswith(prefix):
            return reason.removeprefix(prefix)
    return None


def _user_transaction_key(row: Mapping[str, object]) -> tuple[str, str]:
    return str(row["user_id"]), str(row["reason"])


def analyze_snapshot(
    tasks: Sequence[Mapping[str, object]],
    holds: Sequence[Mapping[str, object]],
    entries: Sequence[Mapping[str, object]],
    *,
    transactions: Sequence[Mapping[str, object]] = (),
    treasuries: Sequence[Mapping[str, object]] = (),
    receipts: Sequence[Mapping[str, object]] = (),
    commands: Sequence[Mapping[str, object]] = (),
) -> list[dict[str, object]]:
    """Return sorted anomalies from immutable row snapshots."""
    anomalies: list[dict[str, object]] = []
    tasks_by_id = {str(row["id"]): row for row in tasks}
    holds_by_id = {str(row["id"]): row for row in holds}
    tasks_by_hold: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    holds_by_task_reason: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    entries_by_hold: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    receipts_by_hold: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    treasuries_by_slug = {
        str(row["resident_slug"]): row
        for row in treasuries
    }
    actual_user_transactions: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {"amount": 0, "count": 0, "transaction_ids": []}
    )
    expected_user_transactions: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {"amount": 0, "count": 0, "hold_ids": set()}
    )

    for task in tasks:
        hold_id = task.get("hold_id")
        task_id = str(task["id"])
        task_status = str(task["status"])
        if hold_id is None:
            if task_status != "draft":
                anomalies.append(
                    _anomaly(
                        "task_missing_hold_binding",
                        task_id=task_id,
                        task_status=task_status,
                    )
                )
            continue
        hold_key = str(hold_id)
        tasks_by_hold[hold_key].append(task)
        hold = holds_by_id.get(hold_key)
        if hold is None:
            anomalies.append(
                _anomaly(
                    "task_references_missing_hold",
                    task_id=task_id,
                    hold_id=hold_key,
                )
            )
            continue
        expected_reason = f"lab_task:{task_id}"
        if hold.get("reason") != expected_reason:
            anomalies.append(
                _anomaly(
                    "task_hold_reason_conflict",
                    task_id=task_id,
                    hold_id=hold_key,
                    task_expected_reason=expected_reason,
                    hold_reason=hold.get("reason"),
                )
            )
        issuer_user_id = task.get("issuer_user_id")
        if (
            isinstance(issuer_user_id, str)
            and issuer_user_id
            and hold.get("user_id") != issuer_user_id
        ):
            anomalies.append(
                _anomaly(
                    "task_hold_owner_conflict",
                    task_id=task_id,
                    hold_id=hold_key,
                    task_issuer_user_id=issuer_user_id,
                    hold_user_id=hold.get("user_id"),
                )
            )

    for hold in holds:
        hold_id = str(hold["id"])
        task_id = _hold_task_id(hold.get("reason"))
        if task_id is None:
            anomalies.append(
                _anomaly(
                    "hold_reason_invalid",
                    hold_id=hold_id,
                    hold_reason=hold.get("reason"),
                )
            )
            continue
        holds_by_task_reason[task_id].append(hold)
        task = tasks_by_id.get(task_id)
        if task is None:
            anomalies.append(
                _anomaly(
                    "hold_references_missing_task",
                    hold_id=hold_id,
                    task_id=task_id,
                )
            )
            continue
        if task.get("hold_id") != hold_id:
            anomalies.append(
                _anomaly(
                    "hold_task_link_conflict",
                    hold_id=hold_id,
                    task_id=task_id,
                    task_hold_id=task.get("hold_id"),
                )
            )

    operation_keys: Counter[str] = Counter()
    recipient_keys: Counter[tuple[str, str, str]] = Counter()
    for entry in entries:
        hold_id = str(entry["hold_id"])
        entries_by_hold[hold_id].append(entry)
        operation_key = str(entry["operation_key"])
        operation_keys[operation_key] += 1
        recipient_keys[
            (hold_id, str(entry["terminal_action"]), str(entry["recipient_key"]))
        ] += 1
        if hold_id not in holds_by_id:
            anomalies.append(
                _anomaly(
                    "journal_references_missing_hold",
                    entry_id=str(entry.get("id", "")),
                    hold_id=hold_id,
                )
            )

    for receipt in receipts:
        hold_id = str(receipt["hold_id"])
        receipts_by_hold[hold_id].append(receipt)
        if hold_id not in holds_by_id:
            anomalies.append(
                _anomaly(
                    "receipt_references_missing_hold",
                    receipt_id=str(receipt.get("receipt_id", "")),
                    hold_id=hold_id,
                )
            )

    for command in commands:
        if command.get("status") != "failed":
            continue
        task_id = str(command.get("task_id", ""))
        hold_id = str(command.get("hold_id", ""))
        task = tasks_by_id.get(task_id)
        hold = holds_by_id.get(hold_id)
        if (
            task is not None
            and hold is not None
            and str(task.get("status")) not in TERMINAL_TASK_STATUSES
            and hold.get("status") == "held"
        ):
            anomalies.append(
                _anomaly(
                    "failed_terminalization_command_has_held_escrow",
                    command_id=str(command.get("command_id", "")),
                    operation=str(command.get("operation", "")),
                    task_id=task_id,
                    hold_id=hold_id,
                )
            )

    for transaction in transactions:
        amount = transaction.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            continue
        reason = transaction.get("reason")
        user_id = transaction.get("user_id")
        if not isinstance(reason, str) or not isinstance(user_id, str):
            continue
        bucket = actual_user_transactions[(user_id, reason)]
        bucket["amount"] += amount
        bucket["count"] += 1
        bucket["transaction_ids"].append(str(transaction.get("id", "")))

    for operation_key, count in operation_keys.items():
        if count > 1:
            anomalies.append(
                _anomaly(
                    "duplicate_operation_key", operation_key=operation_key, count=count
                )
            )
    for (hold_id, action, recipient), count in recipient_keys.items():
        if count > 1:
            anomalies.append(
                _anomaly(
                    "duplicate_terminal_recipient",
                    hold_id=hold_id,
                    terminal_action=action,
                    recipient_key=recipient,
                    count=count,
                )
            )

    for hold_id, linked_tasks in tasks_by_hold.items():
        if len(linked_tasks) > 1:
            anomalies.append(
                _anomaly(
                    "hold_linked_to_multiple_tasks",
                    hold_id=hold_id,
                    task_ids=sorted(str(row["id"]) for row in linked_tasks),
                )
            )
    for task_id, linked_holds in holds_by_task_reason.items():
        if len(linked_holds) > 1:
            anomalies.append(
                _anomaly(
                    "task_linked_to_multiple_holds",
                    task_id=task_id,
                    hold_ids=sorted(str(row["id"]) for row in linked_holds),
                )
            )

    for hold_id, hold in holds_by_id.items():
        linked_tasks = tasks_by_hold.get(hold_id, [])
        task = linked_tasks[0] if len(linked_tasks) == 1 else None
        hold_status = str(hold["status"])
        task_status = str(task["status"]) if task is not None else None
        version = str(hold.get("terminalization_version", "v1"))

        if hold_status == "held" and task_status in TERMINAL_TASK_STATUSES:
            anomalies.append(
                _anomaly(
                    "held_hold_has_terminal_task",
                    hold_id=hold_id,
                    task_id=str(task["id"]),
                    task_status=task_status,
                )
            )
        if hold_status == "settled" and task_status != "completed":
            anomalies.append(
                _anomaly(
                    "settled_hold_task_conflict",
                    hold_id=hold_id,
                    task_id=str(task["id"]) if task else None,
                    task_status=task_status,
                )
            )
        if hold_status == "refunded" and task_status not in REFUND_TASK_STATUSES:
            anomalies.append(
                _anomaly(
                    "refunded_hold_task_conflict",
                    hold_id=hold_id,
                    task_id=str(task["id"]) if task else None,
                    task_status=task_status,
                )
            )

        hold_entries = entries_by_hold.get(hold_id, [])
        hold_receipts = receipts_by_hold.get(hold_id, [])
        if hold_status == "held" and hold_entries:
            anomalies.append(
                _anomaly(
                    (
                        "nonterminal_v2_hold_has_journal"
                        if version == "v2"
                        else "nonterminal_hold_has_journal"
                    ),
                    hold_id=hold_id,
                    entry_count=len(hold_entries),
                )
            )
        if hold_status == "held" and hold_receipts:
            anomalies.append(
                _anomaly(
                    "nonterminal_hold_has_receipt",
                    hold_id=hold_id,
                    receipt_count=len(hold_receipts),
                )
            )
        if hold_status not in {"settled", "refunded"}:
            continue

        expected_action = "settle" if hold_status == "settled" else "refund"
        if version == "v2":
            if not hold_entries:
                anomalies.append(
                    _anomaly(
                        "v2_terminal_hold_missing_journal",
                        hold_id=hold_id,
                        hold_status=hold_status,
                    )
                )
            if not hold_receipts:
                anomalies.append(
                    _anomaly(
                        "v2_terminal_hold_missing_receipt",
                        hold_id=hold_id,
                        hold_status=hold_status,
                    )
                )
            elif len(hold_receipts) > 1:
                anomalies.append(
                    _anomaly(
                        "v2_terminal_hold_multiple_receipts",
                        hold_id=hold_id,
                        receipt_ids=sorted(
                            str(row.get("receipt_id", "")) for row in hold_receipts
                        ),
                    )
                )
            else:
                receipt = hold_receipts[0]
                if int(receipt["amount"]) != int(hold["amount"]):
                    anomalies.append(
                        _anomaly(
                            "receipt_amount_mismatch",
                            hold_id=hold_id,
                            hold_amount=int(hold["amount"]),
                            receipt_amount=int(receipt["amount"]),
                        )
                    )
                if int(receipt["journal_count"]) != len(hold_entries):
                    anomalies.append(
                        _anomaly(
                            "receipt_journal_count_mismatch",
                            hold_id=hold_id,
                            receipt_journal_count=int(receipt["journal_count"]),
                            actual_journal_count=len(hold_entries),
                        )
                    )

        if not hold_entries:
            continue

        actions = sorted({str(row["terminal_action"]) for row in hold_entries})
        journal_total = sum(int(row["amount"]) for row in hold_entries)
        if actions != [expected_action]:
            anomalies.append(
                _anomaly(
                    "terminal_action_conflict",
                    hold_id=hold_id,
                    hold_status=hold_status,
                    journal_actions=actions,
                )
            )
        if journal_total != int(hold["amount"]):
            anomalies.append(
                _anomaly(
                    "hold_journal_conservation_mismatch",
                    hold_id=hold_id,
                    hold_amount=int(hold["amount"]),
                    journal_total=journal_total,
                )
            )

        for entry in hold_entries:
            recipient = str(entry["recipient_key"])
            reason = entry.get("reason")
            if not isinstance(reason, str) or not reason:
                anomalies.append(
                    _anomaly(
                        "journal_entry_missing_reason",
                        hold_id=hold_id,
                        entry_id=str(entry.get("id", "")),
                    )
                )
                continue
            amount = int(entry["amount"])
            if recipient.startswith("treasury:"):
                slug = recipient.removeprefix("treasury:")
                if slug not in treasuries_by_slug:
                    anomalies.append(
                        _anomaly(
                            "missing_treasury_account",
                            hold_id=hold_id,
                            recipient_key=recipient,
                        )
                    )
                continue
            if recipient == "sink":
                continue
            bucket = expected_user_transactions[(recipient, reason)]
            bucket["amount"] += amount
            bucket["count"] += 1
            bucket["hold_ids"].add(hold_id)

    for (user_id, reason), expected in expected_user_transactions.items():
        actual = actual_user_transactions.get((user_id, reason))
        actual_amount = 0 if actual is None else int(actual["amount"])
        actual_count = 0 if actual is None else int(actual["count"])
        if actual_amount != int(expected["amount"]) or actual_count != int(expected["count"]):
            anomalies.append(
                _anomaly(
                    "user_transaction_mismatch",
                    user_id=user_id,
                    reason=reason,
                    hold_ids=sorted(str(value) for value in expected["hold_ids"]),
                    expected_amount=int(expected["amount"]),
                    actual_amount=actual_amount,
                    expected_count=int(expected["count"]),
                    actual_count=actual_count,
                )
            )

    expected_keys = set(expected_user_transactions)
    for (user_id, reason), actual in actual_user_transactions.items():
        task_id = _task_id_from_terminal_reason(reason)
        if task_id is None or (user_id, reason) in expected_keys:
            continue
        anomalies.append(
            _anomaly(
                "unexpected_user_transaction",
                user_id=user_id,
                reason=reason,
                actual_amount=int(actual["amount"]),
                actual_count=int(actual["count"]),
                transaction_ids=sorted(str(value) for value in actual["transaction_ids"]),
            )
        )

    return sorted(anomalies, key=lambda row: json.dumps(row, sort_keys=True))


def _json_value(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _row_dict(row: Mapping[str, object]) -> dict[str, object]:
    return {key: _json_value(value) for key, value in row.items()}


def summarize_snapshot(
    holds: Sequence[Mapping[str, object]],
    entries: Sequence[Mapping[str, object]],
    *,
    transactions: Sequence[Mapping[str, object]],
    treasuries: Sequence[Mapping[str, object]],
    receipts: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    hold_versions = {
        str(row["id"]): str(row.get("terminalization_version", "v1"))
        for row in holds
    }
    terminal_holds = [
        row for row in holds if str(row.get("status")) in {"settled", "refunded"}
    ]

    def terminal_amount(version: str) -> int:
        return sum(
            int(row["amount"])
            for row in terminal_holds
            if str(row.get("terminalization_version", "v1")) == version
        )

    def journal_amount(version: str) -> int:
        return sum(
            int(row["amount"])
            for row in entries
            if hold_versions.get(str(row["hold_id"])) == version
        )

    return {
        "terminal_hold_amount": sum(int(row["amount"]) for row in terminal_holds),
        "v1_terminal_hold_amount": terminal_amount("v1"),
        "v2_terminal_hold_amount": terminal_amount("v2"),
        "journal_amount": sum(int(row["amount"]) for row in entries),
        "v1_journal_amount": journal_amount("v1"),
        "v2_journal_amount": journal_amount("v2"),
        "positive_user_transaction_amount": sum(
            int(row["amount"]) for row in transactions
        ),
        "treasury_balance": sum(int(row["balance_sc"]) for row in treasuries),
        "receipt_amount": sum(int(row["amount"]) for row in receipts),
        "receipt_journal_count": sum(int(row["journal_count"]) for row in receipts),
    }


async def collect_report(database_url: str, *, assert_disposable: bool) -> dict[str, object]:
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+asyncpg":
        raise ValueError("reconciliation requires a postgresql+asyncpg database URL")

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
                    "coin_hold_entries",
                    "transactions",
                    "resident_treasuries",
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
                        "required reconciliation tables are missing: " + ", ".join(missing)
                    )

                tasks = [
                    _row_dict(row)
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT id, status, hold_id, issuer_user_id "
                                "FROM lab_tasks ORDER BY id"
                            )
                        )
                    ).mappings()
                ]
                holds = [
                    _row_dict(row)
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT id, status, amount, reason, user_id, terminalization_version "
                                "FROM coin_holds WHERE reason LIKE 'lab_task:%' ORDER BY id"
                            )
                        )
                    ).mappings()
                ]
                entries = [
                    _row_dict(row)
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT id, hold_id, terminal_action, recipient_key, amount, "
                                "operation_key, reason FROM coin_hold_entries ORDER BY hold_id, id"
                            )
                        )
                    ).mappings()
                ]
                transactions = [
                    _row_dict(row)
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT id, user_id, amount, reason "
                                "FROM transactions WHERE amount > 0 ORDER BY id"
                            )
                        )
                    ).mappings()
                ]
                treasuries = [
                    _row_dict(row)
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT resident_slug, balance_sc "
                                "FROM resident_treasuries ORDER BY resident_slug"
                            )
                        )
                    ).mappings()
                ]
                receipts_present = (
                    await connection.execute(
                        text(
                            "SELECT to_regclass('lab_terminalization_receipts') IS NOT NULL"
                        )
                    )
                ).scalar_one()
                if receipts_present:
                    receipts = [
                        _row_dict(row)
                        for row in (
                            await connection.execute(
                                text(
                                    "SELECT receipt_id, hold_id, amount, journal_count "
                                    "FROM lab_terminalization_receipts ORDER BY hold_id, receipt_id"
                                )
                            )
                        ).mappings()
                    ]
                else:
                    receipts = []
                commands_present = (
                    await connection.execute(
                        text(
                            "SELECT to_regclass('lab_terminalization_commands') IS NOT NULL"
                        )
                    )
                ).scalar_one()
                if commands_present:
                    commands = [
                        _row_dict(row)
                        for row in (
                            await connection.execute(
                                text(
                                    "SELECT command_id, operation, task_id, hold_id, status "
                                    "FROM lab_terminalization_commands ORDER BY command_id"
                                )
                            )
                        ).mappings()
                    ]
                else:
                    commands = []
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()

    anomalies = analyze_snapshot(
        tasks,
        holds,
        entries,
        transactions=transactions,
        treasuries=treasuries,
        receipts=receipts,
        commands=commands,
    )
    return {
        "schema": "simverse.lab.finance-reconciliation.v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "database": str(database),
        "database_url": parsed.render_as_string(hide_password=True),
        "disposable": disposable == "on",
        "read_only": True,
        "counts": {
            "tasks": len(tasks),
            "holds": len(holds),
            "entries": len(entries),
            "transactions": len(transactions),
            "treasuries": len(treasuries),
            "receipts": len(receipts),
            "commands": len(commands),
            "anomalies": len(anomalies),
        },
        "terminalization_totals": summarize_snapshot(
            holds,
            entries,
            transactions=transactions,
            treasuries=treasuries,
            receipts=receipts,
        ),
        "anomalies": anomalies,
        "ok": not anomalies,
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("LAB_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assert-disposable", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.database_url:
        print(json.dumps({"ok": False, "error": "database URL is required"}), file=sys.stderr)
        return 2
    try:
        report = asyncio.run(
            collect_report(args.database_url, assert_disposable=args.assert_disposable)
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2

    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
