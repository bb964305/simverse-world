from __future__ import annotations

import copy
import json

import pytest

import scripts.generate_lab_cohort_matrix as cohort_script
from scripts.generate_lab_cohort_matrix import (
    Cohort,
    CohortError,
    EXPECTED_MATRIX_SIZE,
    FREEZE_DECISION,
    POSITIVE_RULES,
    Rule,
    classify_cohort,
    collect_actual_rows_from_snapshot,
    generate_matrix,
    main as cohort_main,
    map_actual_rows,
)
from scripts.reconcile_lab_finances import analyze_snapshot, summarize_snapshot


def test_complete_matrix_has_exactly_1120_unique_single_rule_tuples():
    matrix = generate_matrix()

    assert matrix["row_count"] == EXPECTED_MATRIX_SIZE == 1120
    assert matrix["unique_tuple_count"] == EXPECTED_MATRIX_SIZE
    tuples = {
        (
            row["task_status"],
            row["hold_status"],
            row["run_status"],
            row["artifact_state"],
        )
        for row in matrix["rows"]
    }
    assert len(tuples) == EXPECTED_MATRIX_SIZE
    assert all(row["rule_id"] and row["action"] for row in matrix["rows"])
    assert matrix["rule_coverage"] == {
        "cohort.active-run-fence.v1": 12,
        "cohort.completed-v1-audit.v1": 1,
        "cohort.freeze-unclassified.v1": 1082,
        "cohort.funded-held-convert.v1": 1,
        "cohort.refunded-v1-audit.v1": 18,
        "cohort.rejected-arbitration.v1": 3,
        "cohort.review-eligible.v1": 2,
        "cohort.v1-draft-read-only.v1": 1,
    }


def test_matrix_is_byte_stable_and_contains_no_runtime_timestamp():
    first = json.dumps(generate_matrix(), sort_keys=True, separators=(",", ":"))
    second = json.dumps(generate_matrix(), sort_keys=True, separators=(",", ":"))

    assert first == second
    assert "generated_at" not in first


def test_overlapping_rule_is_a_hard_error_instead_of_ordered_precedence():
    cohort = Cohort("draft", "none", "none", "none")
    overlapping = (*POSITIVE_RULES, Rule("test.overlap", "convert", lambda row: row == cohort))

    with pytest.raises(CohortError, match="overlapping cohort rules"):
        classify_cohort(cohort, overlapping)


def test_unknown_raw_state_is_never_coerced_to_freeze_bucket():
    with pytest.raises(CohortError, match="unknown task_status"):
        classify_cohort(Cohort("mystery", "held", "none", "none"))


def test_unclassified_canonical_tuple_has_explicit_stable_freeze_rule():
    decision = classify_cohort(Cohort("draft", "held", "running", "v1_candidate"))

    assert decision == FREEZE_DECISION


def test_actual_mapping_freezes_unknown_and_multi_link_rows_and_reports_both():
    rows = [
        {
            "id": "unknown",
            "task_status": "new-upstream-state",
            "hold_status": "held",
            "run_status": "none",
            "artifact_state": "none",
            "task_count": 1,
            "hold_count": 1,
            "run_count": 0,
            "artifact_count": 0,
        },
        {
            "id": "multi-run",
            "task_status": "review",
            "hold_status": "held",
            "run_status": "succeeded",
            "artifact_state": "v1_verified",
            "task_count": 1,
            "hold_count": 1,
            "run_count": 2,
            "artifact_count": 1,
        },
        {
            "id": "missing-run-link",
            "task_status": "review",
            "hold_status": "held",
            "run_status": "succeeded",
            "artifact_state": "v1_verified",
            "task_count": 1,
            "hold_count": 1,
            "run_count": 0,
            "artifact_count": 1,
        },
    ]

    mapped, anomalies = map_actual_rows(rows)

    assert [row["action"] for row in mapped] == ["freeze", "freeze", "freeze"]
    assert {row["id"] for row in anomalies} == {
        "unknown",
        "multi-run",
        "missing-run-link",
    }
    assert any("ambiguous actual-row links" in row["error"] for row in anomalies)


def test_actual_rows_json_mode_hard_fails_on_empty_collection(tmp_path, capsys):
    payload = tmp_path / "actual-rows.json"
    payload.write_text("[]", encoding="utf-8")

    exit_code = cohort_main(["--actual-rows", str(payload)])

    assert exit_code == 1
    assert "actual rows collection is empty" in capsys.readouterr().err


def test_database_actual_rows_mode_hard_fails_on_empty_collection(monkeypatch, capsys):
    async def fake_collect(database_url: str, *, assert_disposable: bool):
        return cohort_script.ActualRowCollection(
            database="simverse_lab_release_fake",
            database_url=database_url,
            disposable=True,
            read_only=True,
            rows=[],
        )

    monkeypatch.setattr(cohort_script, "collect_actual_rows", fake_collect)

    exit_code = cohort_main(
        ["--database-url", "postgresql+asyncpg://tester:secret@db.example/simverse"]
    )

    assert exit_code == 1
    assert "actual rows collection is empty" in capsys.readouterr().err


def test_actual_rows_cli_ignores_environment_database_url_when_json_is_explicit(
    tmp_path, monkeypatch, capsys
):
    payload = tmp_path / "actual-rows.json"
    payload.write_text(
        json.dumps(
            [
                {
                    "id": "row-1",
                    "task_status": "review",
                    "hold_status": "held",
                    "run_status": "succeeded",
                    "artifact_state": "v1_verified",
                    "task_count": 1,
                    "hold_count": 1,
                    "run_count": 1,
                    "artifact_count": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://env-user:env-pass@db/env")
    monkeypatch.setenv(
        "LAB_TEST_DATABASE_URL", "postgresql+asyncpg://env-user:env-pass@db/env"
    )

    exit_code = cohort_main(["--actual-rows", str(payload)])
    stdout, stderr = capsys.readouterr()

    assert exit_code == 0
    assert stderr == ""
    document = json.loads(stdout)
    assert document["actual_row_source"] == {
        "type": "json",
        "path": str(payload),
    }
    assert document["actual_row_summary"] == {
        "row_count": 1,
        "anomaly_count": 0,
        "unresolved_count": 0,
    }


def test_actual_mapping_requires_explicit_counts_and_marks_canonical_freeze_unresolved():
    rows = [
        {
            "id": "freeze-row",
            "task_status": "draft",
            "hold_status": "held",
            "run_status": "running",
            "artifact_state": "v1_candidate",
            "task_count": 1,
            "hold_count": 1,
            "run_count": 1,
            "artifact_count": 1,
        },
        {
            "id": "missing-count",
            "task_status": "review",
            "hold_status": "held",
            "run_status": "succeeded",
            "artifact_state": "v1_verified",
            "task_count": 1,
            "hold_count": 1,
            "artifact_count": 1,
        },
    ]

    mapped, anomalies = map_actual_rows(rows)

    assert [row["action"] for row in mapped] == ["freeze", "freeze"]
    assert {row["id"] for row in anomalies} == {"freeze-row", "missing-count"}
    assert any("unresolved freeze cohort" in row["error"] for row in anomalies)
    assert any("missing multiplicity fields" in row["error"] for row in anomalies)


def test_actual_mapping_freezes_multiple_artifacts_even_when_state_is_canonical():
    rows = [
        {
            "id": "verified-bundle",
            "task_status": "review",
            "hold_status": "held",
            "run_status": "succeeded",
            "artifact_state": "v1_verified",
            "task_count": 1,
            "hold_count": 1,
            "run_count": 1,
            "artifact_count": 2,
        }
    ]

    mapped, anomalies = map_actual_rows(rows)

    assert mapped[0]["action"] == "freeze"
    assert anomalies[0]["id"] == "verified-bundle"
    assert "'artifact_count': {'actual': 2, 'expected': 1}" in anomalies[0]["error"]


def test_actual_row_snapshot_collector_emits_explicit_artifact_multiplicity():
    rows = collect_actual_rows_from_snapshot(
        tasks=[
            {
                "id": "task-1",
                "status": "review",
                "hold_id": "hold-1",
                "accepted_run_id": "run-1",
            }
        ],
        holds=[
            {
                "id": "hold-1",
                "status": "held",
                "reason": "lab_task:task-1",
            }
        ],
        runs=[
            {
                "id": "run-1",
                "task_id": "task-1",
                "status": "succeeded",
            }
        ],
        artifacts=[
            {
                "id": "artifact-1",
                "task_id": "task-1",
                "run_id": "run-1",
                "scan_status": "clean",
                "verification_status": "verified",
            },
            {
                "id": "artifact-2",
                "task_id": "task-1",
                "run_id": "run-1",
                "scan_status": "clean",
                "verification_status": "verified",
            },
        ],
    )

    assert rows == [
        {
            "id": "task:task-1",
            "task_id": "task-1",
            "hold_id": "hold-1",
            "run_id": "run-1",
            "task_status": "review",
            "hold_status": "held",
            "run_status": "succeeded",
            "artifact_state": "v1_verified",
            "task_count": 1,
            "hold_count": 1,
            "run_count": 1,
            "artifact_count": 2,
        }
    ]
    mapped, anomalies = map_actual_rows(rows)

    assert mapped[0]["action"] == "freeze"
    assert anomalies[0]["id"] == "task:task-1"
    assert "'artifact_count': {'actual': 2, 'expected': 1}" in anomalies[0]["error"]


@pytest.mark.parametrize(
    ("artifact", "runs", "expected_error"),
    [
        (
            {"id": "orphan-run", "task_id": "task-1", "run_id": "missing-run"},
            [],
            "artifact run_id does not reference a known run",
        ),
        (
            {"id": "missing-run-none", "task_id": "task-1", "run_id": None},
            [],
            "artifact has no valid run binding",
        ),
        (
            {"id": "missing-run-empty", "task_id": "task-1", "run_id": ""},
            [],
            "artifact has no valid run binding",
        ),
        (
            {"id": "cross-task", "task_id": "task-1", "run_id": "run-2"},
            [{"id": "run-2", "task_id": "task-2", "status": "succeeded"}],
            "artifact task_id does not match its run task_id",
        ),
        (
            {"id": "no-binding", "task_id": None, "run_id": None},
            [],
            "artifact has no valid task binding",
        ),
        (
            {"id": "orphan-task", "task_id": "missing-task", "run_id": None},
            [],
            "artifact task_id does not reference a known task",
        ),
    ],
)
def test_snapshot_collector_emits_fail_closed_rows_for_invalid_artifact_bindings(
    artifact, runs, expected_error
):
    rows = collect_actual_rows_from_snapshot(
        tasks=[
            {
                "id": "task-1",
                "status": "draft",
                "hold_id": None,
                "accepted_run_id": None,
            },
            {
                "id": "task-2",
                "status": "draft",
                "hold_id": None,
                "accepted_run_id": "run-2",
            },
        ],
        holds=[],
        runs=runs,
        artifacts=[
            {
                **artifact,
                "scan_status": "clean",
                "verification_status": "verified",
            }
        ],
    )

    artifact_row = next(row for row in rows if row["id"] == f"artifact:{artifact['id']}")
    assert artifact_row["artifact_count"] == 1
    assert artifact_row["collector_error"] == expected_error

    mapped, anomalies = map_actual_rows(rows)

    assert next(row for row in mapped if row["id"] == artifact_row["id"])["action"] == "freeze"
    assert next(row for row in anomalies if row["id"] == artifact_row["id"])[
        "error"
    ] == expected_error


def test_snapshot_collector_fails_closed_for_extra_or_unbound_runs():
    tasks = [
        {
            "id": "multiple",
            "status": "review",
            "hold_id": "hold-multiple",
            "accepted_run_id": "run-accepted",
        },
        {
            "id": "unbound",
            "status": "funded",
            "hold_id": "hold-unbound",
            "accepted_run_id": None,
        },
    ]
    holds = [
        {
            "id": "hold-multiple",
            "status": "held",
            "reason": "lab_task:multiple",
        },
        {
            "id": "hold-unbound",
            "status": "held",
            "reason": "lab_task:unbound",
        },
    ]
    runs = [
        {"id": "run-accepted", "task_id": "multiple", "status": "succeeded"},
        {"id": "run-extra", "task_id": "multiple", "status": "failed"},
        {"id": "run-unbound", "task_id": "unbound", "status": "queued"},
    ]

    rows = collect_actual_rows_from_snapshot(
        tasks=tasks,
        holds=holds,
        runs=runs,
        artifacts=[],
    )
    mapped, anomalies = map_actual_rows(rows)

    assert [row["run_count"] for row in rows] == [2, 1]
    assert {row["id"] for row in anomalies} == {"task:multiple", "task:unbound"}
    assert all(row["action"] == "freeze" for row in mapped)


def test_finance_reconciliation_accepts_conservative_v2_terminal_snapshot():
    tasks = [
        {
            "id": "task-1",
            "status": "completed",
            "hold_id": "hold-1",
            "issuer_user_id": "issuer",
        }
    ]
    holds = [
        {
            "id": "hold-1",
            "status": "settled",
            "amount": 10,
            "reason": "lab_task:task-1",
            "user_id": "issuer",
            "terminalization_version": "v2",
        }
    ]
    entries = [
        {
            "id": "entry-1",
            "hold_id": "hold-1",
            "terminal_action": "settle",
            "recipient_key": "creator",
            "amount": 6,
            "operation_key": "op-creator",
            "reason": "lab_reward:task-1",
        },
        {
            "id": "entry-2",
            "hold_id": "hold-1",
            "terminal_action": "settle",
            "recipient_key": "sink",
            "amount": 4,
            "operation_key": "op-sink",
            "reason": "lab_fee:task-1",
        },
    ]
    transactions = [
        {
            "id": "txn-1",
            "user_id": "creator",
            "amount": 6,
            "reason": "lab_reward:task-1",
        }
    ]
    receipts = [
        {
            "receipt_id": "receipt-1",
            "hold_id": "hold-1",
            "amount": 10,
            "journal_count": 2,
        }
    ]

    assert (
        analyze_snapshot(
            tasks,
            holds,
            entries,
            transactions=transactions,
            receipts=receipts,
        )
        == []
    )
    assert summarize_snapshot(
        holds,
        entries,
        transactions=transactions,
        treasuries=[{"resident_slug": "researcher", "balance_sc": 0}],
        receipts=receipts,
    ) == {
        "terminal_hold_amount": 10,
        "v1_terminal_hold_amount": 0,
        "v2_terminal_hold_amount": 10,
        "journal_amount": 10,
        "v1_journal_amount": 0,
        "v2_journal_amount": 10,
        "positive_user_transaction_amount": 6,
        "treasury_balance": 0,
        "receipt_amount": 10,
        "receipt_journal_count": 2,
    }


def test_finance_reconciliation_requires_nonempty_v2_journal_even_when_receipt_exists():
    tasks = [
        {
            "id": "task-1",
            "status": "completed",
            "hold_id": "hold-1",
            "issuer_user_id": "issuer",
        }
    ]
    holds = [
        {
            "id": "hold-1",
            "status": "settled",
            "amount": 10,
            "reason": "lab_task:task-1",
            "user_id": "issuer",
            "terminalization_version": "v2",
        }
    ]
    receipts = [
        {
            "receipt_id": "receipt-1",
            "hold_id": "hold-1",
            "amount": 10,
            "journal_count": 0,
        }
    ]

    anomalies = analyze_snapshot(tasks, holds, [], receipts=receipts)

    assert {row["kind"] for row in anomalies} == {"v2_terminal_hold_missing_journal"}


def test_finance_reconciliation_checks_v2_receipt_journal_count_even_when_actual_is_zero():
    tasks = [
        {
            "id": "task-1",
            "status": "completed",
            "hold_id": "hold-1",
            "issuer_user_id": "issuer",
        }
    ]
    holds = [
        {
            "id": "hold-1",
            "status": "settled",
            "amount": 10,
            "reason": "lab_task:task-1",
            "user_id": "issuer",
            "terminalization_version": "v2",
        }
    ]
    receipts = [
        {
            "receipt_id": "receipt-1",
            "hold_id": "hold-1",
            "amount": 10,
            "journal_count": 1,
        }
    ]

    anomalies = analyze_snapshot(tasks, holds, [], receipts=receipts)

    assert {
        "v2_terminal_hold_missing_journal",
        "receipt_journal_count_mismatch",
    }.issubset({row["kind"] for row in anomalies})


def test_finance_reconciliation_reports_failed_command_with_held_escrow():
    tasks = [
        {
            "id": "task-1",
            "status": "running",
            "hold_id": "hold-1",
            "issuer_user_id": "issuer",
        }
    ]
    holds = [
        {
            "id": "hold-1",
            "status": "held",
            "amount": 10,
            "reason": "lab_task:task-1",
            "user_id": "issuer",
            "terminalization_version": "v2",
        }
    ]
    commands = [
        {
            "command_id": "command-1",
            "operation": "fail",
            "task_id": "task-1",
            "hold_id": "hold-1",
            "status": "failed",
        }
    ]

    anomalies = analyze_snapshot(tasks, holds, [], commands=commands)

    assert anomalies == [
        {
            "kind": "failed_terminalization_command_has_held_escrow",
            "command_id": "command-1",
            "operation": "fail",
            "task_id": "task-1",
            "hold_id": "hold-1",
        }
    ]


def test_finance_reconciliation_accepts_default_off_v1_journaled_completion():
    tasks = [
        {
            "id": "task-1",
            "status": "completed",
            "hold_id": "hold-1",
            "issuer_user_id": "issuer",
        }
    ]
    holds = [
        {
            "id": "hold-1",
            "status": "settled",
            "amount": 10,
            "reason": "lab_task:task-1",
            "user_id": "issuer",
            "terminalization_version": "v1",
        }
    ]
    entries = [
        {
            "id": "entry-1",
            "hold_id": "hold-1",
            "terminal_action": "settle",
            "recipient_key": "creator",
            "amount": 6,
            "operation_key": "op-creator",
            "reason": "lab_reward:task-1",
        },
        {
            "id": "entry-2",
            "hold_id": "hold-1",
            "terminal_action": "settle",
            "recipient_key": "sink",
            "amount": 4,
            "operation_key": "op-sink",
            "reason": "lab_fee:task-1",
        },
    ]
    transactions = [
        {
            "id": "txn-1",
            "user_id": "creator",
            "amount": 6,
            "reason": "lab_reward:task-1",
        }
    ]

    assert analyze_snapshot(tasks, holds, entries, transactions=transactions) == []


def test_finance_reconciliation_accepts_pre_migration_v1_audit_only_terminal_row():
    tasks = [
        {
            "id": "task-1",
            "status": "completed",
            "hold_id": "hold-1",
            "issuer_user_id": "issuer",
        }
    ]
    holds = [
        {
            "id": "hold-1",
            "status": "settled",
            "amount": 10,
            "reason": "lab_task:task-1",
            "user_id": "issuer",
            "terminalization_version": "v1",
        }
    ]

    assert analyze_snapshot(tasks, holds, []) == []


def test_finance_reconciliation_reports_orphans_reason_conflicts_and_real_delta_mismatches():
    tasks = [
        {
            "id": "task-missing-hold",
            "status": "funded",
            "hold_id": None,
            "issuer_user_id": "issuer-a",
        },
        {
            "id": "task-settled",
            "status": "completed",
            "hold_id": "hold-settled",
            "issuer_user_id": "issuer-b",
        },
    ]
    holds = [
        {
            "id": "hold-orphan",
            "status": "held",
            "amount": 5,
            "reason": "lab_task:missing-task",
            "user_id": "issuer-z",
            "terminalization_version": "v1",
        },
        {
            "id": "hold-settled",
            "status": "settled",
            "amount": 10,
            "reason": "lab_task:wrong-task",
            "user_id": "wrong-issuer",
            "terminalization_version": "v2",
        },
    ]
    entries = [
        {
            "id": "entry-creator",
            "hold_id": "hold-settled",
            "terminal_action": "settle",
            "recipient_key": "creator",
            "amount": 6,
            "operation_key": "op-creator",
            "reason": "lab_reward:task-settled",
        },
        {
            "id": "entry-treasury",
            "hold_id": "hold-settled",
            "terminal_action": "settle",
            "recipient_key": "treasury:slug-1",
            "amount": 3,
            "operation_key": "op-treasury",
            "reason": "lab_treasury:task-settled",
        },
        {
            "id": "entry-sink",
            "hold_id": "hold-settled",
            "terminal_action": "settle",
            "recipient_key": "sink",
            "amount": 1,
            "operation_key": "op-sink",
            "reason": "lab_fee:task-settled",
        },
    ]
    transactions = [
        {
            "id": "txn-1",
            "user_id": "creator",
            "amount": 5,
            "reason": "lab_reward:task-settled",
        }
    ]

    anomalies = analyze_snapshot(
        tasks,
        holds,
        entries,
        transactions=transactions,
        treasuries=[],
    )

    assert {
        "task_missing_hold_binding",
        "hold_references_missing_task",
        "task_hold_reason_conflict",
        "task_hold_owner_conflict",
        "v2_terminal_hold_missing_receipt",
        "user_transaction_mismatch",
        "missing_treasury_account",
    }.issubset({row["kind"] for row in anomalies})


def test_finance_reconciliation_reports_without_mutating_input_snapshots():
    tasks = [
        {
            "id": "task-1",
            "status": "completed",
            "hold_id": "hold-1",
            "issuer_user_id": "issuer",
        }
    ]
    holds = [
        {
            "id": "hold-1",
            "status": "held",
            "amount": 10,
            "reason": "lab_task:task-1",
            "user_id": "issuer",
            "terminalization_version": "v2",
        }
    ]
    entries = [
        {
            "id": "entry-1",
            "hold_id": "hold-1",
            "terminal_action": "refund",
            "recipient_key": "issuer",
            "amount": 9,
            "operation_key": "duplicate",
            "reason": "lab_cancel:task-1",
        },
        {
            "id": "entry-2",
            "hold_id": "hold-1",
            "terminal_action": "refund",
            "recipient_key": "issuer",
            "amount": 1,
            "operation_key": "duplicate",
            "reason": "lab_cancel:task-1",
        },
    ]
    before = copy.deepcopy((tasks, holds, entries))

    anomalies = analyze_snapshot(tasks, holds, entries)

    assert (tasks, holds, entries) == before
    assert {
        "duplicate_operation_key",
        "duplicate_terminal_recipient",
        "held_hold_has_terminal_task",
        "nonterminal_v2_hold_has_journal",
    }.issubset({row["kind"] for row in anomalies})
