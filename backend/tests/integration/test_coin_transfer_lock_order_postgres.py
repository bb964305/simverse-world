"""Real-PostgreSQL regression for transfer vs breakglass user lock ordering."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.transaction import Transaction
from app.models.user import User
from app.services import coin_service


pytestmark = [pytest.mark.lab_postgres, pytest.mark.anyio]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
TRUE = {"1", "true", "yes", "on"}


def _required_database() -> tuple[str, str]:
    required = os.environ.get("LAB_POSTGRES_REQUIRED", "").lower()
    database_url = os.environ.get("LAB_TEST_DATABASE_URL", "")
    run_id = os.environ.get("LAB_RELEASE_RUN_ID", "")
    if required not in TRUE or not database_url or not run_id:
        pytest.fail(
            "lock-order PG evidence requires LAB_POSTGRES_REQUIRED=true, "
            "LAB_TEST_DATABASE_URL, and LAB_RELEASE_RUN_ID"
        )
    if make_url(database_url).drivername != "postgresql+asyncpg":
        pytest.fail("LAB_TEST_DATABASE_URL must use postgresql+asyncpg")
    return database_url, run_id


@pytest.fixture(scope="module")
def migrated_database() -> tuple[str, str]:
    database_url, run_id = _required_database()
    env = {**os.environ, "DATABASE_URL": database_url, "DEBUG": "true"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "038_add_lab_terminalization_v2"],
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        pytest.fail(
            "lock-order PG migration failed:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return database_url, run_id


async def _call_breakglass(connection, request: dict[str, object]) -> str:
    return (
        await connection.execute(
            text(
                "SELECT public.apply_lab_breakglass_compensation("
                ":operation_key, :ticket, :reason, :actor, :task_id, :hold_id, "
                "CAST(:legs AS jsonb))"
            ),
            {**request, "legs": json.dumps(request["legs"], sort_keys=True)},
        )
    ).scalar_one()


async def _seed_breakglass_race(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, object]:
    prefix = uuid.uuid4().hex
    issuer_id = f"lock-race-issuer-{prefix}"
    user_a = f"lock-race-a-{prefix}"
    user_b = f"lock-race-b-{prefix}"
    task_id = f"lock-race-task-{prefix}"
    transfer_reason = f"race_transfer:{task_id}"
    operation_key = f"breakglass-race:{prefix}"
    ticket = f"LOCK-{prefix[:12]}"
    reason = "correct transfer/breakglass lock-order race"
    actor = "admin:release-race"

    async with factory() as db:
        db.add_all(
            [
                User(
                    id=issuer_id,
                    name=issuer_id,
                    email=f"{issuer_id}@lock-race.test",
                    soul_coin_balance=18,
                ),
                User(
                    id=user_a,
                    name=user_a,
                    email=f"{user_a}@lock-race.test",
                    soul_coin_balance=20,
                ),
                User(
                    id=user_b,
                    name=user_b,
                    email=f"{user_b}@lock-race.test",
                    soul_coin_balance=30,
                ),
            ]
        )
        await db.commit()

    async with factory() as db:
        hold_id = await coin_service.hold(
            db,
            issuer_id,
            18,
            f"lab_task:{task_id}",
            terminalization_version="v1",
        )
        assert hold_id is not None

    async with factory() as db:
        await db.execute(
            text(
                "INSERT INTO lab_tasks("
                "id, issuer_user_id, title, brief_md, reward_sc, platform_fee_sc, "
                "deliverable_kind, status, hold_id, reject_count, deadline_at, "
                "created_at, updated_at"
                ") VALUES ("
                ":task_id, :issuer_id, 'breakglass lock race', '', 18, 0, "
                "'report', 'funded', :hold_id, 0, clock_timestamp() + interval '1 day', "
                "clock_timestamp(), clock_timestamp()"
                ")"
            ),
            {
                "task_id": task_id,
                "issuer_id": issuer_id,
                "hold_id": hold_id,
            },
        )
        await db.commit()

    request = {
        "operation_key": operation_key,
        "ticket": ticket,
        "reason": reason,
        "actor": actor,
        "task_id": task_id,
        "hold_id": hold_id,
        # Reverse the request order on purpose; the function must canonicalize to A then B.
        "legs": [
            {"recipient_key": user_b, "amount_delta": -7},
            {"recipient_key": user_a, "amount_delta": 7},
        ],
    }
    return {
        "issuer_id": issuer_id,
        "user_a": user_a,
        "user_b": user_b,
        "task_id": task_id,
        "hold_id": hold_id,
        "transfer_reason": transfer_reason,
        "operation_key": operation_key,
        "request": request,
    }


async def _snapshot(
    factory: async_sessionmaker[AsyncSession],
    ids: dict[str, object],
) -> dict[str, object]:
    user_ids = [str(ids["issuer_id"]), str(ids["user_a"]), str(ids["user_b"])]
    async with factory() as db:
        balances = dict(
            (
                await db.execute(
                    select(User.id, User.soul_coin_balance).where(User.id.in_(user_ids))
                )
            ).all()
        )
        hold = (
            await db.execute(
                text(
                    "SELECT status, amount FROM coin_holds WHERE id = :hold_id"
                ),
                {"hold_id": ids["hold_id"]},
            )
        ).one()
        task_status = (
            await db.execute(
                text("SELECT status FROM lab_tasks WHERE id = :task_id"),
                {"task_id": ids["task_id"]},
            )
        ).scalar_one()

        audit = (
            await db.execute(
                text(
                    "SELECT audit_id, amount, event_id, payload_json "
                    "FROM lab_breakglass_audits "
                    "WHERE operation_key = :operation_key"
                ),
                {"operation_key": ids["operation_key"]},
            )
        ).mappings().one_or_none()

        entries: list[tuple[str, int, str, int | None, int | None]] = []
        outbox_topic = None
        outbox_payload = None
        audit_id = None
        audit_amount = None
        canonical_legs = None
        audit_count = 0
        compensation_entry_count = 0
        outbox_count = 0

        if audit is not None:
            audit_id = audit["audit_id"]
            audit_amount = audit["amount"]
            payload = audit["payload_json"] or {}
            canonical_legs = payload.get("legs")
            audit_count = 1
            entries = list(
                (
                    await db.execute(
                        text(
                            "SELECT recipient_key, amount_delta, reason, "
                            "account_balance_before, account_balance_after "
                            "FROM lab_compensation_entries "
                            "WHERE audit_id = :audit_id "
                            "ORDER BY recipient_key"
                        ),
                        {"audit_id": audit_id},
                    )
                ).all()
            )
            compensation_entry_count = len(entries)
            outbox = (
                await db.execute(
                    text(
                        "SELECT topic, payload_json "
                        "FROM outbox_events "
                        "WHERE event_id = :event_id"
                    ),
                    {"event_id": audit["event_id"]},
                )
            ).mappings().one_or_none()
            if outbox is not None:
                outbox_count = 1
                outbox_topic = outbox["topic"]
                outbox_payload = outbox["payload_json"]

        hold_entry_count = (
            await db.execute(
                text("SELECT count(*) FROM coin_hold_entries WHERE hold_id = :hold_id"),
                {"hold_id": ids["hold_id"]},
            )
        ).scalar_one()
        receipt_count = (
            await db.execute(
                text(
                    "SELECT count(*) FROM lab_terminalization_receipts "
                    "WHERE hold_id = :hold_id"
                ),
                {"hold_id": ids["hold_id"]},
            )
        ).scalar_one()
        transactions = Counter(
            (
                await db.execute(
                    select(Transaction.user_id, Transaction.amount, Transaction.reason).where(
                        Transaction.user_id.in_(user_ids)
                    )
                )
            ).all()
        )
        return {
            "balances": balances,
            "hold_status": hold.status,
            "hold_amount": hold.amount,
            "task_status": task_status,
            "audit_id": audit_id,
            "audit_amount": audit_amount,
            "audit_count": audit_count,
            "canonical_legs": canonical_legs,
            "entries": entries,
            "compensation_entry_count": compensation_entry_count,
            "outbox_count": outbox_count,
            "outbox_topic": outbox_topic,
            "outbox_payload": outbox_payload,
            "hold_entry_count": hold_entry_count,
            "receipt_count": receipt_count,
            "transactions": transactions,
        }


def _economy_total(snapshot: dict[str, object]) -> int:
    balances = snapshot["balances"]
    assert isinstance(balances, dict)
    held = int(snapshot["hold_amount"]) if snapshot["hold_status"] == "held" else 0
    return sum(int(amount) for amount in balances.values()) + held


async def test_transfer_and_breakglass_share_global_user_lock_order(
    migrated_database,
    monkeypatch,
):
    database_url, _ = migrated_database
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        ids = await _seed_breakglass_race(factory)
        before = await _snapshot(factory, ids)
        assert _economy_total(before) == 68
        assert before["balances"] == {
            ids["issuer_id"]: 0,
            ids["user_a"]: 20,
            ids["user_b"]: 30,
        }
        assert before["hold_status"] == "held"
        assert before["task_status"] == "funded"
        assert before["audit_count"] == 0
        assert before["outbox_count"] == 0
        assert before["hold_entry_count"] == 0
        assert before["receipt_count"] == 0

        original_lock_user_accounts = coin_service.lock_user_accounts
        transfer_locked_users = asyncio.Event()
        release_transfer = asyncio.Event()

        async def blocking_lock_user_accounts(
            db: AsyncSession, user_ids: tuple[str, ...] | list[str]
        ) -> list[str]:
            missing = await original_lock_user_accounts(db, user_ids)
            if (
                set(user_ids) == {str(ids["user_a"]), str(ids["user_b"])}
                and not transfer_locked_users.is_set()
            ):
                transfer_locked_users.set()
                await release_transfer.wait()
            return missing

        monkeypatch.setattr(
            coin_service,
            "lock_user_accounts",
            blocking_lock_user_accounts,
        )

        async def run_transfer() -> bool:
            async with factory() as db:
                return await coin_service.transfer(
                    db,
                    str(ids["user_b"]),
                    str(ids["user_a"]),
                    5,
                    str(ids["transfer_reason"]),
                )

        async def run_breakglass() -> str:
            async with engine.begin() as connection:
                return await _call_breakglass(connection, ids["request"])

        transfer_task = asyncio.create_task(run_transfer())
        await asyncio.wait_for(transfer_locked_users.wait(), timeout=5)

        breakglass_task = asyncio.create_task(run_breakglass())
        await asyncio.sleep(0.1)
        assert breakglass_task.done() is False

        release_transfer.set()
        transfer_ok, audit_id = await asyncio.wait_for(
            asyncio.gather(transfer_task, breakglass_task),
            timeout=15,
        )
        assert transfer_ok is True

        after = await _snapshot(factory, ids)
        assert after["audit_id"] == audit_id
        assert _economy_total(after) == 68
        assert after["balances"] == {
            ids["issuer_id"]: 0,
            ids["user_a"]: 32,
            ids["user_b"]: 18,
        }
        assert after["hold_status"] == "held"
        assert after["task_status"] == "funded"
        assert after["audit_count"] == 1
        assert after["audit_amount"] == 7
        assert after["canonical_legs"] == [
            {"recipient_key": str(ids["user_a"]), "amount_delta": 7},
            {"recipient_key": str(ids["user_b"]), "amount_delta": -7},
        ]
        assert after["entries"] == [
            (str(ids["user_a"]), 7, ids["request"]["reason"], 25, 32),
            (str(ids["user_b"]), -7, ids["request"]["reason"], 25, 18),
        ]
        assert after["compensation_entry_count"] == 2
        assert after["outbox_count"] == 1
        assert after["outbox_topic"] == "lab_run_event"
        assert after["outbox_payload"]["type"] == "lab.finance.compensated"
        assert after["outbox_payload"]["audit_id"] == audit_id
        assert after["outbox_payload"]["operation_key"] == ids["operation_key"]
        assert after["outbox_payload"]["task_id"] == ids["task_id"]
        assert after["outbox_payload"]["hold_id"] == ids["hold_id"]
        assert after["outbox_payload"]["gross_amount"] == 7
        assert after["hold_entry_count"] == 0
        assert after["receipt_count"] == 0
        assert after["transactions"] == Counter(
            {
                (
                    str(ids["issuer_id"]),
                    -18,
                    f"hold:lab_task:{ids['task_id']}",
                ): 1,
                (
                    str(ids["user_b"]),
                    -5,
                    str(ids["transfer_reason"]),
                ): 1,
                (
                    str(ids["user_a"]),
                    5,
                    str(ids["transfer_reason"]),
                ): 1,
                (
                    str(ids["user_a"]),
                    7,
                    f"lab_compensation:{audit_id}",
                ): 1,
                (
                    str(ids["user_b"]),
                    -7,
                    f"lab_compensation:{audit_id}",
                ): 1,
            }
        )
    finally:
        await engine.dispose()
