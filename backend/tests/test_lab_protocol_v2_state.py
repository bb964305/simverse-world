"""Focused protocol-v2 state, queue, and provider-recovery contracts."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, func, select

from app.lab import queue, runtime_sessions
from app.models.lab_run import LabRun
from app.models.lab_lease import LabRunLease
from app.models.lab_control import LabRunControlRequest
from app.models.lab_runtime import (
    LabRuntimeIntent,
    LabRuntimeResult,
    LabRuntimeSession,
    LabRuntimeTurn,
)


class RecordingProvider:
    def __init__(
        self,
        *,
        crash_create: bool = False,
        lose_host: bool = False,
        name: str = "recording-provider",
    ):
        self.name = name
        self.crash_create = crash_create
        self.lose_host = lose_host
        self.handshake_calls = 0
        self.create_calls: list[tuple[str, int]] = []
        self.reattach_calls: list[tuple[str, int]] = []

    async def handshake(self):
        self.handshake_calls += 1
        return {
            "schema_version": 2,
            "protocol_version": 2,
            "provider_name": self.name,
            "durability_class": "session_affine",
            "reattach_capability": "client_run_id",
            "effect_mode": "broker_only",
            "capabilities": ["broker_mediation"],
        }

    async def create_session(self, *, client_run_id: str, epoch: int):
        self.create_calls.append((client_run_id, epoch))
        if self.crash_create:
            raise RuntimeError("provider response lost after create")
        return {
            "locator": f"runtime:{client_run_id}",
            "session_id": f"session:{client_run_id}",
            "durability_class": "session_affine",
        }

    async def reattach_session(self, *, client_run_id: str, epoch: int):
        self.reattach_calls.append((client_run_id, epoch))
        if self.lose_host:
            return None
        return {
            "locator": f"runtime:{client_run_id}",
            "session_id": f"session:{client_run_id}",
            "durability_class": "session_affine",
        }


async def _add_v2_run_with_lease(
    db,
    *,
    run_id: str,
    epoch: int,
    owner_id: str = "runner-owner",
) -> None:
    now = datetime.now(UTC)
    db.add_all(
        [
            LabRun(
                id=run_id,
                task_id=f"task-{run_id}",
                researcher_slug="sage",
                adapter="simverse_ref",
                protocol_version=2,
            ),
            LabRunLease(
                run_id=run_id,
                owner_id=owner_id,
                fencing_epoch=epoch,
                heartbeat_at=now,
                expires_at=now + timedelta(minutes=5),
            ),
        ]
    )
    await db.commit()


@pytest.mark.parametrize("value", [None, True, False, 0, 3, "1", "v2"])
def test_queue_keys_reject_noncanonical_protocol_versions(value):
    with pytest.raises(ValueError, match="protocol_version"):
        queue.queue_keys(value)


def test_queue_keys_are_the_approved_physical_split():
    assert queue.queue_keys(1) == ("sv:lab:v1:queue", "sv:lab:v1:processing")
    assert queue.queue_keys(2) == ("sv:lab:v2:queue", "sv:lab:v2:processing")


@pytest.mark.anyio
async def test_protocol_split_refuses_undrained_legacy_queue():
    from app.redis_client import get_redis

    redis = get_redis()
    await redis.lpush(queue.LEGACY_QUEUE_KEYS[0], "legacy-run")
    with pytest.raises(queue.LegacyQueueNotDrained, match="legacy Lab queues"):
        await queue.require_legacy_queues_drained()
    await redis.lrem(queue.LEGACY_QUEUE_KEYS[0], 0, "legacy-run")
    await queue.require_legacy_queues_drained()


def _unique_columns(model) -> set[tuple[str, ...]]:
    return {
        tuple(constraint.columns.keys())
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _foreign_key_binding(
    model, name: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    constraints = [
        constraint
        for constraint in model.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint) and constraint.name == name
    ]
    assert len(constraints) == 1
    return (
        tuple(element.parent.name for element in constraints[0].elements),
        tuple(element.target_fullname for element in constraints[0].elements),
    )


def test_gateway_state_has_canonical_uniqueness_boundaries():
    assert {
        ("run_id",),
        ("client_run_id", "fencing_epoch"),
        ("id", "fencing_epoch"),
    } <= _unique_columns(LabRuntimeSession)
    assert {
        ("id", "session_id"),
        ("session_id", "turn_id"),
        ("session_id", "sequence"),
    } <= _unique_columns(LabRuntimeTurn)
    assert {
        ("session_id", "intent_id"),
        ("action_id",),
        (
            "id",
            "session_id",
            "runtime_turn_id",
            "intent_id",
            "action_id",
            "fencing_epoch",
        ),
    } <= _unique_columns(LabRuntimeIntent)
    assert {
        ("command_id",),
        ("receipt_id",),
        ("runtime_intent_id",),
        ("session_id", "intent_id"),
    } <= _unique_columns(LabRuntimeResult)
    assert {("idempotency_key",), ("active_key",)} <= _unique_columns(
        LabRunControlRequest
    )


def test_gateway_state_cross_row_bindings_are_database_constraints():
    assert _foreign_key_binding(
        LabRuntimeIntent, "fk_lab_runtime_intents_session_epoch"
    ) == (
        ("session_id", "fencing_epoch"),
        ("lab_runtime_sessions.id", "lab_runtime_sessions.fencing_epoch"),
    )
    assert _foreign_key_binding(
        LabRuntimeIntent, "fk_lab_runtime_intents_turn_session"
    ) == (
        ("runtime_turn_id", "session_id"),
        ("lab_runtime_turns.id", "lab_runtime_turns.session_id"),
    )
    assert _foreign_key_binding(
        LabRuntimeResult, "fk_lab_runtime_results_intent_binding"
    ) == (
        (
            "runtime_intent_id",
            "session_id",
            "runtime_turn_id",
            "intent_id",
            "action_id",
            "fencing_epoch",
        ),
        (
            "lab_runtime_intents.id",
            "lab_runtime_intents.session_id",
            "lab_runtime_intents.runtime_turn_id",
            "lab_runtime_intents.intent_id",
            "lab_runtime_intents.action_id",
            "lab_runtime_intents.fencing_epoch",
        ),
    )


def test_runtime_result_receipt_and_ack_are_one_database_state():
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in LabRuntimeResult.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    expression = checks["ck_lab_runtime_results_receipt_ack_pair"]
    assert "receipt_id IS NULL AND runtime_acked_at IS NULL" in expression
    assert "receipt_id IS NOT NULL AND runtime_acked_at IS NOT NULL" in expression


@pytest.mark.anyio
async def test_provider_registration_commits_then_exact_retry_is_local(db_session):
    await _add_v2_run_with_lease(
        db_session, run_id="runtime-ready-run", epoch=7
    )
    provider = RecordingProvider()

    ready = await runtime_sessions.create_or_reattach(
        db_session,
        run_id="runtime-ready-run",
        epoch=7,
        owner_id="runner-owner",
        provider=provider,
        durability_class="session_affine",
    )
    same = await runtime_sessions.create_or_reattach(
        db_session,
        run_id="runtime-ready-run",
        epoch=7,
        owner_id="runner-owner",
        provider=provider,
        durability_class="session_affine",
    )

    assert ready.status == "ready"
    assert ready.provider_name == "recording-provider"
    assert same.id == ready.id
    assert provider.create_calls == [(ready.client_run_id, 7)]
    assert provider.reattach_calls == [(ready.client_run_id, 7)]


@pytest.mark.anyio
async def test_completed_session_reattaches_for_same_epoch_finalization(db_session):
    await _add_v2_run_with_lease(
        db_session, run_id="runtime-completed-recovery-run", epoch=8
    )
    provider = RecordingProvider()
    ready = await runtime_sessions.create_or_reattach(
        db_session,
        run_id="runtime-completed-recovery-run",
        epoch=8,
        owner_id="runner-owner",
        provider=provider,
        durability_class="session_affine",
    )
    ready.status = "completed"
    ready.ended_at = datetime.now(UTC)
    await db_session.commit()

    recovered = await runtime_sessions.create_or_reattach(
        db_session,
        run_id="runtime-completed-recovery-run",
        epoch=8,
        owner_id="runner-owner",
        provider=provider,
        durability_class="session_affine",
    )

    assert recovered.id == ready.id
    assert recovered.status == "completed"
    assert len(provider.create_calls) == 1
    assert provider.reattach_calls == []


@pytest.mark.anyio
async def test_ready_session_takeover_transfers_authority_without_provider_recreate(
    db_session,
):
    run_id = "runtime-ready-takeover-run"
    await _add_v2_run_with_lease(db_session, run_id=run_id, epoch=7)
    provider = RecordingProvider()
    ready = await runtime_sessions.create_or_reattach(
        db_session,
        run_id=run_id,
        epoch=7,
        owner_id="runner-owner",
        provider=provider,
        durability_class="session_affine",
    )
    ready_id = ready.id
    lease = await db_session.get(LabRunLease, run_id)
    lease.owner_id = "takeover-owner"
    lease.fencing_epoch = 8
    lease.heartbeat_at = datetime.now(UTC)
    lease.expires_at = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()

    recovered = await runtime_sessions.recover_existing_for_new_authority(
        db_session,
        run_id=run_id,
        authority_epoch=8,
        owner_id="takeover-owner",
        provider=provider,
        durability_class="session_affine",
    )

    assert recovered.id == ready_id
    assert recovered.status == "ready"
    assert recovered.fencing_epoch == 7
    assert recovered.authority_epoch == 8
    assert provider.handshake_calls == 2
    assert len(provider.create_calls) == 1
    assert provider.reattach_calls == []


@pytest.mark.anyio
async def test_runtime_session_rejects_provider_rebinding(db_session):
    await _add_v2_run_with_lease(
        db_session, run_id="runtime-provider-run", epoch=5
    )

    await runtime_sessions.create_or_reattach(
        db_session,
        run_id="runtime-provider-run",
        epoch=5,
        owner_id="runner-owner",
        provider=RecordingProvider(name="provider-a"),
        durability_class="session_affine",
    )
    replacement = RecordingProvider(name="provider-b")
    with pytest.raises(runtime_sessions.RuntimeSessionError, match="provider binding"):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-provider-run",
            epoch=5,
            owner_id="runner-owner",
            provider=replacement,
            durability_class="session_affine",
        )
    assert replacement.create_calls == []
    assert replacement.reattach_calls == []


@pytest.mark.anyio
async def test_runtime_session_requires_provider_owned_reattach_handshake(db_session):
    await _add_v2_run_with_lease(
        db_session, run_id="runtime-handshake-run", epoch=1
    )

    class CreateOnlyProvider:
        name = "create-only"

        async def handshake(self):
            return {
                "schema_version": 2,
                "protocol_version": 2,
                "provider_name": self.name,
                "durability_class": "session_affine",
                "reattach_capability": "client_run_id",
                "effect_mode": "broker_only",
                "capabilities": ["broker_mediation"],
            }

        async def create_session(self, **_kwargs):
            raise AssertionError("handshake gate must reject before create")

    with pytest.raises(runtime_sessions.RuntimeSessionError, match="reattach"):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-handshake-run",
            epoch=1,
            owner_id="runner-owner",
            provider=CreateOnlyProvider(),
            durability_class="session_affine",
        )
    count = await db_session.scalar(
        select(func.count()).select_from(LabRuntimeSession)
    )
    assert count == 0


@pytest.mark.anyio
async def test_provider_result_must_repeat_verified_durability(db_session):
    await _add_v2_run_with_lease(
        db_session, run_id="runtime-result-binding-run", epoch=2
    )

    class MissingDurabilityProvider(RecordingProvider):
        async def create_session(self, *, client_run_id: str, epoch: int):
            self.create_calls.append((client_run_id, epoch))
            return {
                "locator": f"runtime:{client_run_id}",
                "session_id": f"session:{client_run_id}",
            }

    provider = MissingDurabilityProvider()
    with pytest.raises(runtime_sessions.RuntimeSessionError, match="durability_class"):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-result-binding-run",
            epoch=2,
            owner_id="runner-owner",
            provider=provider,
            durability_class="session_affine",
        )
    session = (
        await db_session.execute(
            select(LabRuntimeSession).where(
                LabRuntimeSession.run_id == "runtime-result-binding-run"
            )
        )
    ).scalar_one()
    assert session.status == "quarantined"


@pytest.mark.anyio
async def test_lost_create_response_reattaches_registered_session(db_session):
    await _add_v2_run_with_lease(
        db_session, run_id="runtime-recovery-run", epoch=11
    )
    provider = RecordingProvider(crash_create=True)

    with pytest.raises(RuntimeError, match="response lost"):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-recovery-run",
            epoch=11,
            owner_id="runner-owner",
            provider=provider,
            durability_class="session_affine",
        )
    registered = (
        await db_session.execute(
            select(LabRuntimeSession).where(
                LabRuntimeSession.run_id == "runtime-recovery-run"
            )
        )
    ).scalar_one()
    assert registered.status == "creating"

    provider.crash_create = False
    ready = await runtime_sessions.create_or_reattach(
        db_session,
        run_id="runtime-recovery-run",
        epoch=11,
        owner_id="runner-owner",
        provider=provider,
        durability_class="session_affine",
    )
    assert ready.status == "ready"
    assert len(provider.create_calls) == 1
    assert provider.reattach_calls == [(registered.client_run_id, 11)]


@pytest.mark.anyio
async def test_session_affine_host_loss_quarantines_without_second_create(db_session):
    await _add_v2_run_with_lease(
        db_session, run_id="runtime-host-loss-run", epoch=3
    )
    provider = RecordingProvider(crash_create=True, lose_host=True)

    with pytest.raises(RuntimeError, match="response lost"):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-host-loss-run",
            epoch=3,
            owner_id="runner-owner",
            provider=provider,
            durability_class="session_affine",
        )
    with pytest.raises(runtime_sessions.RuntimeSessionQuarantined):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-host-loss-run",
            epoch=3,
            owner_id="runner-owner",
            provider=provider,
            durability_class="session_affine",
        )

    session = (
        await db_session.execute(
            select(LabRuntimeSession).where(
                LabRuntimeSession.run_id == "runtime-host-loss-run"
            )
        )
    ).scalar_one()
    assert session.status == "quarantined"
    assert len(provider.create_calls) == 1
    assert len(provider.reattach_calls) == 1


@pytest.mark.anyio
async def test_ready_retry_probes_host_and_quarantines_loss(db_session):
    await _add_v2_run_with_lease(
        db_session, run_id="runtime-ready-loss-run", epoch=6
    )
    provider = RecordingProvider()
    ready = await runtime_sessions.create_or_reattach(
        db_session,
        run_id="runtime-ready-loss-run",
        epoch=6,
        owner_id="runner-owner",
        provider=provider,
        durability_class="session_affine",
    )
    assert ready.status == "ready"
    ready_id = ready.id

    provider.lose_host = True
    with pytest.raises(runtime_sessions.RuntimeSessionQuarantined, match="host was lost"):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-ready-loss-run",
            epoch=6,
            owner_id="runner-owner",
            provider=provider,
            durability_class="session_affine",
        )
    db_session.expire_all()
    session = await db_session.get(LabRuntimeSession, ready_id)
    assert session.status == "quarantined"


@pytest.mark.anyio
async def test_runtime_session_requires_a_live_owned_lease_before_handshake(db_session):
    db_session.add(
        LabRun(
            id="runtime-no-lease-run",
            task_id="runtime-no-lease-task",
            researcher_slug="sage",
            adapter="simverse_ref",
            protocol_version=2,
        )
    )
    await db_session.commit()
    provider = RecordingProvider()

    with pytest.raises(runtime_sessions.RuntimeSessionError, match="live lease"):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-no-lease-run",
            epoch=0,
            owner_id="runner-owner",
            provider=provider,
            durability_class="session_affine",
        )
    assert provider.handshake_calls == 0
    assert provider.create_calls == []
    assert provider.reattach_calls == []
    assert await db_session.scalar(
        select(func.count()).select_from(LabRuntimeSession)
    ) == 0


@pytest.mark.anyio
async def test_runtime_session_rejects_v2_mock_adapter_before_provider_effect(db_session):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            LabRun(
                id="runtime-mock-adapter-run",
                task_id="runtime-mock-adapter-task",
                researcher_slug="sage",
                adapter="mock",
                protocol_version=2,
            ),
            LabRunLease(
                run_id="runtime-mock-adapter-run",
                owner_id="runner-owner",
                fencing_epoch=0,
                heartbeat_at=now,
                expires_at=now + timedelta(minutes=5),
            ),
        ]
    )
    await db_session.commit()
    provider = RecordingProvider()

    with pytest.raises(runtime_sessions.RuntimeSessionError, match="simverse_ref"):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-mock-adapter-run",
            epoch=0,
            owner_id="runner-owner",
            provider=provider,
            durability_class="session_affine",
        )
    assert provider.handshake_calls == 0
    assert provider.create_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_locator", [{}, "", [], False, 0])
async def test_provider_binding_rejects_empty_or_non_string_locator(
    db_session, invalid_locator
):
    await _add_v2_run_with_lease(
        db_session, run_id="runtime-invalid-locator-run", epoch=8
    )

    class InvalidLocatorProvider(RecordingProvider):
        async def create_session(self, *, client_run_id: str, epoch: int):
            self.create_calls.append((client_run_id, epoch))
            return {
                "locator": invalid_locator,
                "durability_class": "session_affine",
            }

    provider = InvalidLocatorProvider()
    with pytest.raises(runtime_sessions.RuntimeSessionError, match="locator"):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-invalid-locator-run",
            epoch=8,
            owner_id="runner-owner",
            provider=provider,
            durability_class="session_affine",
        )

    session = (
        await db_session.execute(
            select(LabRuntimeSession).where(
                LabRuntimeSession.run_id == "runtime-invalid-locator-run"
            )
        )
    ).scalar_one()
    assert session.status == "quarantined"


@pytest.mark.anyio
async def test_runtime_session_rejects_v1_run_before_provider_effect(db_session):
    db_session.add(
        LabRun(
            id="runtime-v1-run",
            task_id="runtime-v1-task",
            researcher_slug="sage",
            protocol_version=1,
        )
    )
    await db_session.commit()
    provider = RecordingProvider()

    with pytest.raises(runtime_sessions.RuntimeSessionError, match="protocol_version 2"):
        await runtime_sessions.create_or_reattach(
            db_session,
            run_id="runtime-v1-run",
            epoch=0,
            owner_id="runner-owner",
            provider=provider,
            durability_class="session_affine",
        )
    assert provider.create_calls == []
