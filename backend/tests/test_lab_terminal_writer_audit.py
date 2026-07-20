from pathlib import Path

from scripts.audit_lab_terminal_writers import (
    EXPECTED_FINDINGS,
    Finding,
    _parse_spike,
    source_findings,
    audit,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_terminal_writer_inventory_has_no_unknown_or_missing_sites():
    assert source_findings(REPO_ROOT) == EXPECTED_FINDINGS


def test_d1a_comparison_preserves_one_financial_domain():
    result = audit(REPO_ROOT)
    comparison = result["comparative_matrix"]
    a_prime = comparison["a_prime"]["counts"]
    option_b = comparison["b"]["counts"]

    assert comparison["decision_rule_passed"] is True
    assert a_prime["financial_domains"] == 1
    assert a_prime["backfills"] < option_b["backfills"]
    assert a_prime["services"] <= option_b["services"]
    assert result["unknown_findings"] == []
    assert result["missing_findings"] == []


def test_d1a_strict_decision_requires_external_spike_evidence():
    result = audit(REPO_ROOT)
    assert result["decision"] == "STOP_AND_REASSESS"
    assert result["hard_oracles"]["controlled_postgres_entrypoint_spike"] is False
    assert result["hard_oracles"]["physical_queue_split_spike"] is False


def test_source_inventory_detects_indirect_and_dynamic_terminal_writes(tmp_path):
    source = tmp_path / "backend/app/lab/indirect.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from app.services.coin_service import refund as repay\n"
        "async def mutate(db, run, status):\n"
        "    alias = repay\n"
        "    await alias(db, 'hold', 'reason')\n"
        "    run.status = status\n"
        "    setattr(run, 'status', 'failed')\n"
        "    await db.execute(update(Run).values(status='cancelled'))\n",
        encoding="utf-8",
    )

    findings = source_findings(tmp_path)
    assert Finding("call", "backend/app/lab/indirect.py", "mutate", "refund") in findings
    assert Finding("write", "backend/app/lab/indirect.py", "mutate", "run.status", "<dynamic>") in findings
    assert Finding(
        "write", "backend/app/lab/indirect.py", "mutate", "setattr(run.status)", "failed"
    ) in findings
    assert Finding("write", "backend/app/lab/indirect.py", "mutate", "values.status", "cancelled") in findings


def test_spike_oracles_are_parsed_independently():
    queue_only = _parse_spike(
        b"redis_version=7.4.9\nphysical_queue_cross_claim=PASS v1_second=empty\nfailure_count=0\n"
    )
    assert queue_only["physical_queue_split_passed"] is True
    assert queue_only["postgres_role_guard_passed"] is False

    postgres_only = _parse_spike(
        b"legacy_direct_dml=PASS\nterminalizer_direct_dml=PASS\n"
        b"legacy_function_execute=PASS\nterminalizer_set_role_owner=PASS\n"
        b"terminalizer_controlled_entrypoint=PASS\n"
        b"lab_financial_kernel_owner|f|f|f|f\npostgres_membership_oracle:\n0\n"
        b"postgres_final_state:\nsettled|completed\nfailure_count=0\n"
    )
    assert postgres_only["postgres_role_guard_passed"] is True
    assert postgres_only["physical_queue_split_passed"] is False
