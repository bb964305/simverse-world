"""Real-Postgres protocol-v2 Gateway supervision regressions (Approved-v10 P3).

These probes assert the durable Gateway-side truth over a disposable release
database at Alembic head. Developer runs skip cleanly when that environment is
absent; setting ``LAB_POSTGRES_REQUIRED=1`` turns every missing prerequisite
into a hard failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.lab import broker, protocol, supervision
from app.models.lab_action import LabToolAction
from app.models.lab_event import LabRunEvent
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_runtime import (
    LabRuntimeIntent,
    LabRuntimeResult,
    LabRuntimeSession,
    LabRuntimeTurn,
)
from app.models.lab_task import LabTask


pytestmark = [pytest.mark.lab_postgres, pytest.mark.anyio]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
HEAD_REVISION = "042_lab_world_fencing"
OWNER = "gateway-v2-owner"
EPOCH = 7
SENTINEL = "PG-BROKER-SENTINEL-9F41"
_REQUIRED = os.environ.get("LAB_POSTGRES_REQUIRED", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _require_or_skip(reason: str) -> None:
    if _REQUIRED:
        pytest.fail(f"LAB_POSTGRES_REQUIRED=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def postgres_url() -> str:
    url = os.environ.get("LAB_TEST_DATABASE_URL", "")
    if not url:
        _require_or_skip("LAB_TEST_DATABASE_URL is absent")
    if not url.startswith(("postgresql+asyncpg://", "postgresql://")):
        _require_or_skip("LAB_TEST_DATABASE_URL is not a PostgreSQL URL")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@pytest.fixture(scope="module")
def migrated_postgres_url(postgres_url: str) -> str:
    env = dict(os.environ)
    env["DATABASE_URL"] = postgres_url
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    if completed.returncode != 0:
        _require_or_skip(f"alembic upgrade head failed:\n{completed.stdout[-3000:]}")
    return postgres_url


@pytest.fixture
async def pg_factory(migrated_postgres_url: str):
    engine = create_async_engine(migrated_postgres_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            if conn.dialect.name != "postgresql":
                _require_or_skip(f"connected dialect is {conn.dialect.name!r}")
            database, disposable, revision, result_columns = (
                await conn.execute(
                    text(
                        "SELECT current_database(), "
                        "current_setting('simverse.release_disposable', true), "
                        "(SELECT version_num FROM alembic_version), "
                        "(SELECT count(*) FROM information_schema.columns "
                        " WHERE table_schema='public' "
                        " AND table_name='lab_runtime_results' "
                        " AND column_name IN ('receipt_id','runtime_acked_at'))"
                    )
                )
            ).one()
            release_run_id = os.environ.get("LAB_RELEASE_RUN_ID", "")
            expected_database = f"simverse_lab_release_{release_run_id}"
            if _REQUIRED and (
                not release_run_id
                or database != expected_database
                or disposable != "on"
                or revision != HEAD_REVISION
                or result_columns != 2
            ):
                pytest.fail(
                    "gateway-v2 PG tests require the exact disposable head schema: "
                    f"database={database!r}, expected={expected_database!r}, "
                    f"disposable={disposable!r}, revision={revision!r}, "
                    f"result_columns={result_columns}"
                )
        yield async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
    finally:
        await engine.dispose()


async def _seed_runtime(
    factory,
    *,
    run_id: str,
    owner_id: str = OWNER,
    epoch: int = EPOCH,
) -> dict[str, str | int]:
    provider_session_id = f"provider-{run_id}"
    session_row_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"simverse:test-runtime:{run_id}")
    )
    async with factory() as db:
        task = LabTask(
            id=f"task-{run_id}",
            issuer_user_id="tenant",
            researcher_slug="sage",
            title="protocol-v2 postgres task",
            brief_md="exercise gateway supervision",
            scopes_json=["web_search"],
            status="running",
            accepted_run_id=run_id,
            deliverable_kind="report",
        )
        run = LabRun(
            id=run_id,
            task_id=f"task-{run_id}",
            researcher_slug="sage",
            adapter="simverse_ref",
            status="running",
            protocol_version=2,
            scopes_json=["web_search"],
        )
        lease = LabRunLease(
            run_id=run_id,
            owner_id=owner_id,
            fencing_epoch=epoch,
            heartbeat_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session = LabRuntimeSession(
            id=session_row_id,
            run_id=run_id,
            client_run_id=f"client-{run_id}",
            fencing_epoch=epoch,
            protocol_version=2,
            provider_name="simverse_ref",
            provider_session_id=provider_session_id,
            locator_json={"session_id": provider_session_id},
            durability_class="session_affine",
            status="ready",
        )
        db.add_all([task, run, lease, session])
        await db.commit()
    return {
        "run_id": run_id,
        "task_id": f"task-{run_id}",
        "session_row_id": session_row_id,
        "provider_session_id": provider_session_id,
        "epoch": epoch,
        "owner_id": owner_id,
    }


def _event(
    seeded: dict[str, str | int],
    cursor: int,
    *,
    kind: str = "think",
    turn_id: str | None = None,
    intent_id: str | None = None,
    outcome: str | None = None,
    payload: dict | None = None,
) -> protocol.RuntimeEvent:
    body: dict[str, object] = {
        "schema_version": 2,
        "event_id": f"event-{seeded['run_id']}-{cursor}",
        "run_id": seeded["run_id"],
        "session_id": seeded["provider_session_id"],
        "cursor": cursor,
        "epoch": seeded["epoch"],
        "event_kind": kind,
        "turn_id": turn_id,
        "intent_id": intent_id,
        "outcome": outcome,
        "payload": payload or {"summary": f"event-{cursor}"},
        "occurred_at": datetime.now(UTC),
    }
    if kind == "tool_intent":
        args = {"query": "approved-v10 sentinel"}
        body.update(
            tool_name="web.search",
            tool_args=args,
            tool_args_digest=protocol.args_digest(args),
        )
    return protocol.RuntimeEvent.model_validate(body)


def _action(
    seeded: dict[str, str | int],
    *,
    action_id: str,
    tool_name: str,
    args_hash: str,
    args_json: dict,
    status: str = "succeeded",
    result_json: dict | None = None,
) -> LabToolAction:
    return LabToolAction(
        id=action_id,
        tenant_id="tenant",
        run_id=str(seeded["run_id"]),
        task_id=str(seeded["task_id"]),
        tool_name=tool_name,
        args_hash=args_hash,
        args_redacted_json=args_json,
        risk_class="R1",
        status=status,
        fencing_epoch=int(seeded["epoch"]),
        policy_version="lab-policy-v2",
        idempotency_key=f"idem-{action_id}",
        result_json=result_json or {"sentinel": SENTINEL},
    )


def _receipt(
    command: protocol.ToolResultCommand,
    *,
    receipt_id: str | None = None,
) -> dict[str, str]:
    if receipt_id is None:
        receipt_id = f"runtime-receipt-{command.command_id}"
    return {
        "receipt_id": receipt_id,
        "request_digest": protocol.content_digest(command.model_dump(mode="json")),
        "session_id": command.session_id,
        "turn_id": command.turn_id,
        "intent_id": command.intent_id,
        "action_id": command.action_id,
        "state": "runtime_acked",
    }


@pytest.mark.anyio
async def test_runtime_events_commit_before_ack_and_fail_closed_on_divergent_replay(
    pg_factory,
):
    seeded = await _seed_runtime(pg_factory, run_id=f"pe-{uuid.uuid4().hex}")
    gap_event = _event(seeded, 2, payload={"summary": "gap-first"})
    intent_event = _event(
        seeded,
        1,
        kind="tool_intent",
        turn_id="turn-1",
        intent_id="intent-1",
    )

    async with pg_factory() as db:
        gap_commit = await supervision.commit_runtime_event(
            db, event=gap_event, owner_id=OWNER
        )
        assert gap_commit.duplicate is False
        assert gap_commit.committed_through == 0

        first_commit = await supervision.commit_runtime_event(
            db, event=intent_event, owner_id=OWNER
        )
        assert first_commit.duplicate is False
        assert first_commit.committed_through == 2

        replay = await supervision.commit_runtime_event(
            db, event=intent_event, owner_id=OWNER
        )
        assert replay.duplicate is True
        assert replay.committed_through == 2

        db.expire_all()
        session = await db.get(LabRuntimeSession, seeded["session_row_id"])
        assert session is not None
        assert session.provider_cursor_committed == 2
        assert session.provider_cursor_acked == 0
        assert await db.scalar(
            select(func.count()).select_from(LabRunEvent).where(
                LabRunEvent.run_id == seeded["run_id"]
            )
        ) == 2
        assert await db.scalar(
            select(func.count()).select_from(LabRuntimeTurn).where(
                LabRuntimeTurn.session_id == seeded["session_row_id"]
            )
        ) == 1
        assert await db.scalar(
            select(func.count()).select_from(LabRuntimeIntent).where(
                LabRuntimeIntent.session_id == seeded["session_row_id"]
            )
        ) == 1

        with pytest.raises(supervision.RuntimeProtocolConflict):
            await supervision.record_provider_ack(
                db,
                run_id=str(seeded["run_id"]),
                session_id="wrong-session",
                epoch=EPOCH,
                owner_id=OWNER,
                acked_through=1,
            )
        await db.rollback()
        with pytest.raises(supervision.RuntimeProtocolConflict):
            await supervision.record_provider_ack(
                db,
                run_id=str(seeded["run_id"]),
                session_id=str(seeded["provider_session_id"]),
                epoch=EPOCH + 1,
                owner_id=OWNER,
                acked_through=1,
            )
        await db.rollback()
        with pytest.raises(supervision.RuntimeProtocolConflict):
            await supervision.record_provider_ack(
                db,
                run_id=str(seeded["run_id"]),
                session_id=str(seeded["provider_session_id"]),
                epoch=EPOCH,
                owner_id=OWNER,
                acked_through=3,
            )
        await db.rollback()

        await supervision.record_provider_ack(
            db,
            run_id=str(seeded["run_id"]),
            session_id=str(seeded["provider_session_id"]),
            epoch=EPOCH,
            owner_id=OWNER,
            acked_through=2,
        )
        db.expire_all()
        session = await db.get(LabRuntimeSession, seeded["session_row_id"])
        assert session is not None
        assert session.provider_cursor_acked == 2

        changed_replay = intent_event.model_copy(
            update={"payload": {"summary": "divergent-replay"}}
        )
        with pytest.raises(supervision.RuntimeProtocolConflict):
            await supervision.commit_runtime_event(
                db, event=changed_replay, owner_id=OWNER
            )
        await db.rollback()
        with pytest.raises(supervision.RuntimeProtocolConflict):
            await supervision.commit_runtime_event(
                db,
                event=intent_event.model_copy(update={"session_id": "wrong-session"}),
                owner_id=OWNER,
            )
        await db.rollback()
        with pytest.raises(supervision.RuntimeProtocolConflict):
            await supervision.commit_runtime_event(
                db,
                event=intent_event.model_copy(update={"cursor": 3, "epoch": EPOCH + 1}),
                owner_id=OWNER,
            )
        await db.rollback()


@pytest.mark.anyio
async def test_real_postgres_backpressure_is_durable_and_replay_resumes_at_ack_plus_one(
    pg_factory,
):
    seeded = await _seed_runtime(
        pg_factory, run_id=f"pb-{uuid.uuid4().hex}"
    )

    async with pg_factory() as db:
        for cursor in range(1, protocol.MAX_UNACKED_EVENTS + 1):
            committed = await supervision.commit_runtime_event(
                db, event=_event(seeded, cursor), owner_id=OWNER
            )
            assert committed.committed_through == cursor

        with pytest.raises(supervision.Backpressure):
            await supervision.commit_runtime_event(
                db,
                event=_event(seeded, protocol.MAX_UNACKED_EVENTS + 1),
                owner_id=OWNER,
            )
        await db.rollback()

        db.expire_all()
        session = await db.get(LabRuntimeSession, seeded["session_row_id"])
        assert session is not None
        assert session.provider_cursor_committed == protocol.MAX_UNACKED_EVENTS
        assert session.provider_cursor_acked == 0
        assert await db.scalar(
            select(func.count()).select_from(LabRunEvent).where(
                LabRunEvent.run_id == seeded["run_id"]
            )
        ) == protocol.MAX_UNACKED_EVENTS

        await supervision.record_provider_ack(
            db,
            run_id=str(seeded["run_id"]),
            session_id=str(seeded["provider_session_id"]),
            epoch=EPOCH,
            owner_id=OWNER,
            acked_through=protocol.MAX_UNACKED_EVENTS,
        )
        db.expire_all()
        session = await db.get(LabRuntimeSession, seeded["session_row_id"])
        assert session is not None
        assert session.provider_cursor_acked == protocol.MAX_UNACKED_EVENTS
        assert session.provider_cursor_acked + 1 == protocol.MAX_UNACKED_EVENTS + 1

        resumed_event = _event(seeded, protocol.MAX_UNACKED_EVENTS + 1)
        resumed = await supervision.commit_runtime_event(
            db,
            event=resumed_event,
            owner_id=OWNER,
        )
        assert resumed.committed_through == protocol.MAX_UNACKED_EVENTS + 1
        replay = await supervision.commit_runtime_event(
            db,
            event=resumed_event,
            owner_id=OWNER,
        )
        assert replay.duplicate is True
        assert replay.committed_through == protocol.MAX_UNACKED_EVENTS + 1


@pytest.mark.anyio
async def test_runtime_results_stay_pending_until_receipt_and_gate_finalization(
    pg_factory,
):
    seeded = await _seed_runtime(pg_factory, run_id=f"pr-{uuid.uuid4().hex}")
    intent_event = _event(
        seeded,
        1,
        kind="tool_intent",
        turn_id="turn-result",
        intent_id="intent-result",
    )

    async with pg_factory() as db:
        committed = await supervision.commit_runtime_event(
            db, event=intent_event, owner_id=OWNER
        )
        assert committed.intent_row_id is not None
        assert not await supervision.runtime_final_ready(
            db, session_id=str(seeded["session_row_id"]), require_real_result=True
        )

        first_action = _action(
            seeded,
            action_id=str(uuid.uuid4()),
            tool_name=str(intent_event.tool_name),
            args_hash=str(intent_event.tool_args_digest),
            args_json=dict(intent_event.tool_args or {}),
            result_json={"sentinel": SENTINEL, "source": "postgres"},
        )
        db.add(first_action)
        await db.commit()

        command = await broker.persist_runtime_result(
            db,
            session_id=str(seeded["session_row_id"]),
            intent_row_id=str(committed.intent_row_id),
            action=first_action,
            owner_id=OWNER,
        )
        retry = await broker.persist_runtime_result(
            db,
            session_id=str(seeded["session_row_id"]),
            intent_row_id=str(committed.intent_row_id),
            action=first_action,
            owner_id=OWNER,
        )
        assert retry.command_id == command.command_id

        result_row = await db.scalar(
            select(LabRuntimeResult).where(
                LabRuntimeResult.command_id == command.command_id
            )
        )
        assert result_row is not None
        assert result_row.receipt_id is None
        assert result_row.runtime_acked_at is None
        assert result_row.payload_json == {"sentinel": SENTINEL, "source": "postgres"}

        intent_row = await db.get(LabRuntimeIntent, committed.intent_row_id)
        turn_row = await db.get(LabRuntimeTurn, committed.turn_row_id)
        assert intent_row is not None
        assert turn_row is not None
        assert intent_row.status == "result_recorded"
        assert turn_row.status == "result_recorded"
        assert not await supervision.runtime_final_ready(
            db, session_id=str(seeded["session_row_id"]), require_real_result=True
        )

        with pytest.raises(supervision.RuntimeProtocolConflict):
            await supervision.commit_runtime_event(
                db,
                event=_event(
                    seeded,
                    2,
                    kind="final",
                    turn_id="turn-final",
                    payload={"summary": "blocked until receipt"},
                ),
                owner_id=OWNER,
            )
        await db.rollback()

        divergent_action = _action(
            seeded,
            action_id=str(uuid.uuid4()),
            tool_name=str(intent_event.tool_name),
            args_hash=str(intent_event.tool_args_digest),
            args_json=dict(intent_event.tool_args or {}),
            result_json={"sentinel": "DIFFERENT"},
        )
        db.add(divergent_action)
        await db.commit()
        with pytest.raises(broker.RuntimeResultConflict):
            await broker.persist_runtime_result(
                db,
                session_id=str(seeded["session_row_id"]),
                intent_row_id=str(committed.intent_row_id),
                action=divergent_action,
                owner_id=OWNER,
            )
        await db.rollback()

        receipt = _receipt(command)
        recorded = await supervision.record_runtime_result_receipt(
            db, command=command, receipt=receipt, owner_id=OWNER
        )
        assert recorded.receipt_id == receipt["receipt_id"]
        exact_retry = await supervision.record_runtime_result_receipt(
            db, command=command, receipt=receipt, owner_id=OWNER
        )
        assert exact_retry.receipt_id == receipt["receipt_id"]

        with pytest.raises(supervision.RuntimeProtocolConflict):
            await supervision.record_runtime_result_receipt(
                db,
                command=command,
                receipt=_receipt(command, receipt_id="different-receipt"),
                owner_id=OWNER,
            )
        await db.rollback()
        with pytest.raises(supervision.RuntimeProtocolConflict):
            await supervision.record_runtime_result_receipt(
                db,
                command=command.model_copy(update={"session_id": "wrong-session"}),
                receipt={
                    **receipt,
                    "session_id": "wrong-session",
                    "request_digest": protocol.content_digest(
                        command.model_copy(
                            update={"session_id": "wrong-session"}
                        ).model_dump(mode="json")
                    ),
                },
                owner_id=OWNER,
            )
        await db.rollback()
        with pytest.raises(supervision.RuntimeProtocolConflict):
            await supervision.record_runtime_result_receipt(
                db,
                command=command.model_copy(update={"epoch": EPOCH + 1}),
                receipt={
                    **receipt,
                    "request_digest": protocol.content_digest(
                        command.model_copy(
                            update={"epoch": EPOCH + 1}
                        ).model_dump(mode="json")
                    ),
                },
                owner_id=OWNER,
            )
        await db.rollback()

        db.expire_all()
        result_row = await db.scalar(
            select(LabRuntimeResult).where(
                LabRuntimeResult.command_id == command.command_id
            )
        )
        intent_row = await db.get(LabRuntimeIntent, committed.intent_row_id)
        turn_row = await db.get(LabRuntimeTurn, committed.turn_row_id)
        assert result_row is not None
        assert intent_row is not None
        assert turn_row is not None
        assert result_row.receipt_id == receipt["receipt_id"]
        assert result_row.runtime_acked_at is not None
        assert intent_row.status == "runtime_acked"
        assert turn_row.status == "runtime_acked"
        assert await supervision.runtime_final_ready(
            db, session_id=str(seeded["session_row_id"]), require_real_result=True
        )

        result_commit = await supervision.commit_runtime_event(
            db,
            event=_event(
                seeded,
                2,
                kind="tool_result",
                turn_id=command.turn_id,
                intent_id=command.intent_id,
                outcome=command.outcome,
                payload=command.payload,
            ),
            owner_id=OWNER,
        )
        assert result_commit.duplicate is False
        think_commit = await supervision.commit_runtime_event(
            db,
            event=_event(
                seeded,
                3,
                turn_id="turn-final",
                payload={"summary": "compose final"},
            ),
            owner_id=OWNER,
        )
        assert think_commit.duplicate is False
        final_commit = await supervision.commit_runtime_event(
            db,
            event=_event(
                seeded,
                4,
                kind="final",
                turn_id="turn-final",
                payload={"summary": "runtime completed after real receipt"},
            ),
            owner_id=OWNER,
        )
        assert final_commit.duplicate is False
        db.expire_all()
        session = await db.get(LabRuntimeSession, seeded["session_row_id"])
        final_turn = await db.scalar(
            select(LabRuntimeTurn).where(
                LabRuntimeTurn.session_id == seeded["session_row_id"],
                LabRuntimeTurn.turn_id == "turn-final",
            )
        )
        assert session is not None
        assert final_turn is not None
        assert session.status == "completed"
        assert session.ended_at is not None
        assert final_turn.status == "final"
