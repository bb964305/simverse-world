"""AC12 machine oracle for externally executed staging global-kill drills.

``LAB_KILL_DRILL`` points at the immutable JSON receipt produced by the staging
controller.  This test validates the full receipt, not merely its existence.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest


pytestmark = [pytest.mark.lab_staging]
_TRUE = {"1", "true", "yes", "on"}
_EFFECTS = {"runtime", "broker", "executor", "world", "old_action"}


def _required_receipt() -> dict:
    if os.environ.get("LAB_STAGING_REQUIRED", "").lower() not in _TRUE:
        pytest.skip("AC12 staging evidence was not requested")
    missing = [
        name
        for name in (
            "LAB_KILL_DRILL",
            "LAB_RUNTIME_BASE_URL",
            "LAB_EXECUTOR_BASE_URL",
            "LAB_SHA",
        )
        if not os.environ.get(name)
    ]
    if missing:
        pytest.fail("required AC12 environment is incomplete: " + ", ".join(missing))
    path = Path(os.environ["LAB_KILL_DRILL"]).resolve()
    if not path.is_file():
        pytest.fail(f"LAB_KILL_DRILL is not a receipt file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        pytest.fail(f"invalid LAB_KILL_DRILL receipt: {exc}")


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assert_inventory(drill: dict) -> list[dict]:
    assert drill["admission"]["before_open"] is True
    assert drill["admission"]["after_open"] is False
    assert drill["admission"]["after_epoch"] == drill["admission"]["before_epoch"] + 1
    assert drill["admission"]["fence_latency_ms"] <= 2000
    assert drill["target_watermark_committed_with_admission"] is True
    assert drill["missing_run_ids"] == []
    targets = drill["targets"]
    assert targets
    assert {target["kind"] for target in targets} == {"runtime", "executor"}
    assert len({(target["run_id"], target["kind"], target["target_id"]) for target in targets}) == len(targets)
    assert all(target["epoch"] == drill["admission"]["after_epoch"] for target in targets)
    return targets


def test_nominal_global_kill_confirms_every_target_without_quarantine():
    receipt = _required_receipt()
    assert receipt["schema"] == "simverse.lab.global-kill.v1"
    assert receipt["tested_sha"] == os.environ["LAB_SHA"]
    assert receipt["runtime_base_url"] == os.environ["LAB_RUNTIME_BASE_URL"]
    assert receipt["executor_base_url"] == os.environ["LAB_EXECUTOR_BASE_URL"]

    targets = _assert_inventory(receipt["nominal"])
    assert receipt["nominal"]["quarantine_count"] == 0
    assert all(target["status"] == "confirmed_stopped" for target in targets)
    for target in targets:
        assert target["receipt_id"]
        assert _timestamp(target["stopped_at"]) <= _timestamp(target["control_deadline"]) + timedelta(seconds=5)


def test_fault_global_kill_quarantines_only_injected_targets_and_denies_all_stale_effects():
    receipt = _required_receipt()
    fault = receipt["fault"]
    targets = _assert_inventory(fault)
    injected = set(fault["injected_unreachable_target_ids"])
    quarantined = {target["target_id"] for target in targets if target["status"] == "quarantined"}
    confirmed = {target["target_id"] for target in targets if target["status"] == "confirmed_stopped"}

    assert injected
    assert quarantined == injected
    assert fault["quarantine_count"] == len(injected)
    assert confirmed == {target["target_id"] for target in targets} - injected
    assert all(target.get("stopped_at") is None for target in targets if target["target_id"] in injected)
    probes = fault["stale_effect_probes"]
    assert {probe["effect"] for probe in probes} == _EFFECTS
    assert all(probe["decision"] == "denied_stale_epoch" for probe in probes)
    assert all(probe["observed_effect_count"] == 0 for probe in probes)
