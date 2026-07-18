"""T1 — Simverse Lab Runtime Protocol v1 contract + storage freeze (PRD
§Protocols, §Data and API Evolution, P0). Pure contract/storage tests only —
no execution logic (Broker/Policy/Lease land in later tasks).
"""
from datetime import datetime, timedelta, UTC

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.lab.protocol import (
    PROTOCOL_VERSION,
    MAX_EVENT_BYTES,
    EVENT_TYPES,
    RunEventEnvelope,
    GrantClaims,
    HandshakeManifest,
    ProtocolError,
    canonical_json,
    args_digest,
    validate_handshake,
)
from app.models.lab_event import LabRunEvent, OutboxEvent
from app.models.lab_grant import LabCapabilityGrant  # noqa: F401 (registers table)
from app.models.lab_action import LabToolAction, LabApproval
from app.models.lab_lease import LabRunLease  # noqa: F401 (registers table)
from app.models.lab_budget import LabRunBudget  # noqa: F401 (registers table)
from app.models.world_revision import WorldRevision  # noqa: F401 (registers table)


# ─── 1. canonical_json / args_digest ─────────────────────────────


def test_canonical_json_ignores_key_order():
    a = {"b": 1, "a": 2, "c": {"y": 1, "x": 2}}
    b = {"a": 2, "c": {"x": 2, "y": 1}, "b": 1}
    assert canonical_json(a) == canonical_json(b)


def test_canonical_json_nested_dicts_are_stable():
    nested_a = {"outer": {"z": 1, "a": {"m": 2, "l": 3}}}
    nested_b = {"outer": {"a": {"l": 3, "m": 2}, "z": 1}}
    assert canonical_json(nested_a) == canonical_json(nested_b)


def test_args_digest_stable_for_equivalent_args():
    args1 = {"x": 1, "nested": {"a": 1, "b": 2}}
    args2 = {"nested": {"b": 2, "a": 1}, "x": 1}
    assert args_digest(args1) == args_digest(args2)


def test_args_digest_changes_when_nested_value_changes():
    base = {"x": 1, "nested": {"a": 1, "b": 2}}
    changed = {"x": 1, "nested": {"a": 1, "b": 3}}
    assert args_digest(base) != args_digest(changed)


# ─── 2. RunEventEnvelope ──────────────────────────────────────────


def _envelope_kwargs(**overrides):
    kwargs = dict(
        event_id="evt-1", tenant_id="t1", run_id="r1", task_id="task1",
        seq=1, type="run.started", actor="runtime", fencing_epoch=0,
        policy_version="lab-policy-v1", occurred_at=datetime.now(UTC),
    )
    kwargs.update(overrides)
    return kwargs


def test_run_event_envelope_rejects_unknown_type():
    assert "not.a.real.type" not in EVENT_TYPES
    with pytest.raises(ValidationError):
        RunEventEnvelope(**_envelope_kwargs(type="not.a.real.type"))


def test_run_event_envelope_rejects_oversized_payload():
    huge_payload = {"blob": "x" * (MAX_EVENT_BYTES + 1)}
    with pytest.raises(ValidationError):
        RunEventEnvelope(**_envelope_kwargs(payload=huge_payload))


def test_run_event_envelope_accepts_valid_event():
    env = RunEventEnvelope(**_envelope_kwargs())
    assert env.schema_version == 1
    assert env.type == "run.started"
    assert env.seq == 1


# ─── 3. GrantClaims ───────────────────────────────────────────────


def _grant_kwargs(**overrides):
    now = int(datetime.now(UTC).timestamp())
    kwargs = dict(
        iss="lab-runtime", aud="tool-broker", jti="jti-1", tenant_id="t1",
        task_id="task1", run_id="r1", agent_id="agent-1", depth=0,
        capabilities=["web_search"], budgets={"model_tokens": 1000},
        policy_version="lab-policy-v1", fencing_epoch=0, nbf=now, exp=now + 900,
    )
    kwargs.update(overrides)
    return kwargs


def test_grant_claims_rejects_wrong_audience():
    with pytest.raises(ValidationError):
        GrantClaims(**_grant_kwargs(aud="something-else"))


def test_grant_claims_rejects_depth_over_one():
    with pytest.raises(ValidationError):
        GrantClaims(**_grant_kwargs(depth=2))


def test_grant_claims_rejects_exp_not_after_nbf():
    now = int(datetime.now(UTC).timestamp())
    with pytest.raises(ValidationError):
        GrantClaims(**_grant_kwargs(nbf=now, exp=now))


def test_grant_claims_accepts_valid_claims():
    grant = GrantClaims(**_grant_kwargs())
    assert grant.aud == "tool-broker"
    assert grant.depth == 0


# ─── 4. validate_handshake ────────────────────────────────────────


def _manifest_kwargs(**overrides):
    kwargs = dict(
        protocol_version=PROTOCOL_VERSION, runtime="mock", runtime_version="1.0",
        capabilities=["broker_mediation"],
    )
    kwargs.update(overrides)
    return kwargs


def test_validate_handshake_rejects_wrong_protocol_version():
    manifest = HandshakeManifest(**_manifest_kwargs(protocol_version=PROTOCOL_VERSION + 1))
    with pytest.raises(ProtocolError):
        validate_handshake(manifest)


def test_validate_handshake_rejects_missing_broker_mediation_capability():
    manifest = HandshakeManifest(**_manifest_kwargs(capabilities=["something_else"]))
    with pytest.raises(ProtocolError):
        validate_handshake(manifest)


def test_validate_handshake_accepts_valid_manifest():
    manifest = HandshakeManifest(**_manifest_kwargs())
    validate_handshake(manifest)  # must not raise


# ─── 5. LabRunEvent / OutboxEvent DB round-trip ──────────────────


@pytest.mark.anyio
async def test_lab_run_event_duplicate_run_seq_rejected(db_session):
    now = datetime.now(UTC)
    e1 = LabRunEvent(
        tenant_id="t1", run_id="run-1", task_id="task-1", seq=1,
        type="run.started", actor="runtime", policy_version="lab-policy-v1",
        occurred_at=now,
    )
    db_session.add(e1)
    await db_session.commit()

    e2 = LabRunEvent(
        tenant_id="t1", run_id="run-1", task_id="task-1", seq=1,
        type="run.completed", actor="runtime", policy_version="lab-policy-v1",
        occurred_at=now,
    )
    db_session.add(e2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.anyio
async def test_lab_run_event_duplicate_provider_event_id_rejected(db_session):
    now = datetime.now(UTC)
    e1 = LabRunEvent(
        tenant_id="t1", run_id="run-2", task_id="task-1", seq=1,
        type="run.started", actor="runtime", policy_version="lab-policy-v1",
        occurred_at=now, provider_event_id="prov-1",
    )
    db_session.add(e1)
    await db_session.commit()

    e2 = LabRunEvent(
        tenant_id="t1", run_id="run-2", task_id="task-1", seq=2,
        type="run.completed", actor="runtime", policy_version="lab-policy-v1",
        occurred_at=now, provider_event_id="prov-1",
    )
    db_session.add(e2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.anyio
async def test_lab_run_event_null_provider_event_id_can_repeat(db_session):
    now = datetime.now(UTC)
    e1 = LabRunEvent(
        tenant_id="t1", run_id="run-3", task_id="task-1", seq=1,
        type="run.started", actor="runtime", policy_version="lab-policy-v1",
        occurred_at=now, provider_event_id=None,
    )
    e2 = LabRunEvent(
        tenant_id="t1", run_id="run-3", task_id="task-1", seq=2,
        type="run.completed", actor="runtime", policy_version="lab-policy-v1",
        occurred_at=now, provider_event_id=None,
    )
    db_session.add_all([e1, e2])
    await db_session.commit()  # must not raise


@pytest.mark.anyio
async def test_outbox_event_autoincrement_id_monotonic(db_session):
    o1 = OutboxEvent(event_id="evt-a", tenant_id="t1", topic="lab.run", payload_json={})
    db_session.add(o1)
    await db_session.commit()
    await db_session.refresh(o1)

    o2 = OutboxEvent(event_id="evt-b", tenant_id="t1", topic="lab.run", payload_json={})
    db_session.add(o2)
    await db_session.commit()
    await db_session.refresh(o2)

    assert o2.id > o1.id


# ─── 6. LabToolAction / LabApproval unique constraints ───────────


@pytest.mark.anyio
async def test_lab_tool_action_idempotency_key_unique(db_session):
    a1 = LabToolAction(
        tenant_id="t1", run_id="run-1", task_id="task-1", tool_name="web.search",
        args_hash="hash1", risk_class="R0", policy_version="lab-policy-v1",
        idempotency_key="idem-1",
    )
    db_session.add(a1)
    await db_session.commit()

    a2 = LabToolAction(
        tenant_id="t1", run_id="run-1", task_id="task-1", tool_name="web.search",
        args_hash="hash2", risk_class="R0", policy_version="lab-policy-v1",
        idempotency_key="idem-1",
    )
    db_session.add(a2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.anyio
async def test_lab_approval_action_id_unique(db_session):
    now = datetime.now(UTC)
    ap1 = LabApproval(
        tenant_id="t1", run_id="run-1", task_id="task-1", action_id="action-1",
        args_digest="digest1", expires_at=now + timedelta(seconds=60),
    )
    db_session.add(ap1)
    await db_session.commit()

    ap2 = LabApproval(
        tenant_id="t1", run_id="run-1", task_id="task-1", action_id="action-1",
        args_digest="digest2", expires_at=now + timedelta(seconds=60),
    )
    db_session.add(ap2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ─── 8. settings ──────────────────────────────────────────────────


def test_lab_agent_v1_settings_defaults():
    from app.config import settings
    assert settings.lab_agent_v1_enabled is False
    assert settings.lab_grant_secret == ""
    assert settings.lab_grant_ttl_s == 900
    assert settings.lab_policy_version == "lab-policy-v1"
    assert settings.lab_budget_model_tokens == 200_000
    assert settings.lab_budget_tool_calls == 100
    assert settings.lab_budget_wall_clock_ms == 1_200_000
    assert settings.lab_budget_egress_requests == 200
    assert settings.lab_budget_egress_bytes == 104_857_600
    assert settings.lab_budget_artifact_count == 20
    assert settings.lab_budget_artifact_bytes == 104_857_600
    assert settings.lab_budget_active_workers == 3
