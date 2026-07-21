"""Regression contract for atomic Lab escrow terminalization.

These tests intentionally exercise the public finance/task entry points rather
than inspecting implementation symbols.  They lock the P1 invariants from the
Approved-v10 plan: invalid distributions write nothing, a split failure cannot
leave an earlier credit behind, and task/hold/balance/outbox terminal effects
commit or roll back as one unit.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.coin_hold import CoinHold
from app.models.coin_hold_entry import CoinHoldEntry
from app.models.lab_event import OutboxEvent
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.lab_terminalization import (
    LabTerminalizationCommand,
    LabTerminalizationReceipt,
)
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.models.transaction import Transaction
from app.models.user import User
from app.services import coin_service
from app.services import lab_task_service
from app.services import lab_terminalization_service
from scripts.reconcile_lab_finances import analyze_snapshot


class InjectedSplitFailure(RuntimeError):
    """Fault raised after one distribution credit has completed."""


class InjectedTerminalCommitFailure(RuntimeError):
    """Fault raised at the commit that includes a terminal task state."""


class InjectedRefundFailure(RuntimeError):
    """Fault raised before a terminal refund can credit the issuer."""


@pytest.fixture(autouse=True)
def _ready_v2_terminalizer(monkeypatch):
    monkeypatch.setattr(settings, "lab_terminalizer_v2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "lab_terminalizer_worker_enabled", True, raising=False)
    monkeypatch.setattr(
        settings,
        "lab_terminalizer_database_url",
        "sqlite+aiosqlite:///dedicated-terminalizer.db",
        raising=False,
    )


def _factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_hold(
    factory: async_sessionmaker[AsyncSession],
    *,
    amount: int = 10,
    recipients: Sequence[str] = ("creator",),
) -> str:
    async with factory() as db:
        db.add(
            User(
                id="issuer",
                name="Issuer",
                email="issuer@finance.test",
                soul_coin_balance=amount,
            )
        )
        for recipient in recipients:
            db.add(
                User(
                    id=recipient,
                    name=recipient,
                    email=f"{recipient}@finance.test",
                    soul_coin_balance=0,
                )
            )
        await db.commit()

    async with factory() as db:
        hold_id = await coin_service.hold(db, "issuer", amount, "lab_task:finance-test")
        assert hold_id is not None
        return hold_id


async def _finance_snapshot(
    factory: async_sessionmaker[AsyncSession], hold_id: str
) -> dict[str, object]:
    """Read committed state through a fresh session, bypassing identity maps."""
    async with factory() as db:
        hold = await db.get(CoinHold, hold_id)
        assert hold is not None
        users = (await db.execute(select(User.id, User.soul_coin_balance))).all()
        transactions = (
            await db.execute(select(Transaction.user_id, Transaction.amount, Transaction.reason))
        ).all()
        treasuries = (
            await db.execute(
                select(ResidentTreasury.resident_slug, ResidentTreasury.balance_sc)
            )
        ).all()
        outbox = (
            await db.execute(select(OutboxEvent.event_id, OutboxEvent.topic))
        ).all()
        return {
            "hold": (hold.status, hold.settled_at),
            "users": sorted((str(user_id), balance) for user_id, balance in users),
            "transactions": sorted(
                (str(user_id), amount, reason) for user_id, amount, reason in transactions
            ),
            "treasuries": sorted(
                (str(slug), balance) for slug, balance in treasuries
            ),
            "outbox": sorted((str(event_id), topic) for event_id, topic in outbox),
        }


INVALID_SPLITS = [
    pytest.param([], id="empty"),
    pytest.param(
        [("creator", 0, "zero"), ("sink", 10, "remainder")],
        id="zero-amount",
    ),
    pytest.param(
        [("creator", -1, "negative"), ("sink", 11, "remainder")],
        id="negative-amount",
    ),
    pytest.param(
        [("creator", 5, "first"), ("creator", 5, "duplicate")],
        id="duplicate-recipient",
    ),
    pytest.param([("missing-user", 10, "missing")], id="missing-user"),
    pytest.param([("treasury:", 10, "empty-slug")], id="invalid-treasury"),
    pytest.param([(None, 10, "null-recipient")], id="null-recipient"),
    pytest.param(
        [("creator", 5.5, "fractional"), ("sink", 4.5, "remainder")],
        id="non-integer-amount",
    ),
]


@pytest.mark.parametrize("task_exists", [True, False])
def test_reconciliation_flags_terminal_credit_without_expected_journal(task_exists):
    task_id = "unjournaled-task" if task_exists else "missing-task"
    tasks = (
        [
            {
                "id": task_id,
                "status": "completed",
                "hold_id": "unjournaled-hold",
                "issuer_user_id": "issuer",
            }
        ]
        if task_exists
        else []
    )
    holds = (
        [
            {
                "id": "unjournaled-hold",
                "user_id": "issuer",
                "amount": 10,
                "status": "settled",
                "reason": f"lab_task:{task_id}",
                "terminalization_version": "v1",
            }
        ]
        if task_exists
        else []
    )

    anomalies = analyze_snapshot(
        tasks,
        holds,
        [],
        transactions=[
            {
                "id": f"transaction-{task_id}",
                "user_id": "recipient",
                "amount": 10,
                "reason": f"lab_reward:{task_id}",
            }
        ],
    )

    assert [row for row in anomalies if row["kind"] == "unexpected_user_transaction"] == [
        {
            "kind": "unexpected_user_transaction",
            "user_id": "recipient",
            "reason": f"lab_reward:{task_id}",
            "actual_amount": 10,
            "actual_count": 1,
            "transaction_ids": [f"transaction-{task_id}"],
        }
    ]


def test_reconciliation_flags_terminal_credit_with_empty_task_binding():
    anomalies = analyze_snapshot(
        [],
        [],
        [],
        transactions=[
            {
                "id": "transaction-empty-task",
                "user_id": "recipient",
                "amount": 7,
                "reason": "lab_reward:",
            }
        ],
    )

    assert [row for row in anomalies if row["kind"] == "unexpected_user_transaction"] == [
        {
            "kind": "unexpected_user_transaction",
            "user_id": "recipient",
            "reason": "lab_reward:",
            "actual_amount": 7,
            "actual_count": 1,
            "transaction_ids": ["transaction-empty-task"],
        }
    ]


@pytest.mark.anyio
async def test_invalid_hold_cohort_is_rejected_before_debit(db_engine):
    factory = _factory(db_engine)
    async with factory() as db:
        db.add(
            User(
                id="invalid-cohort-issuer",
                name="Invalid cohort issuer",
                email="invalid-cohort@finance.test",
                soul_coin_balance=10,
            )
        )
        await db.commit()

    async with factory() as db:
        with pytest.raises(coin_service.CoinError, match="must be v1 or v2"):
            await coin_service.hold_pending(
                db,
                "invalid-cohort-issuer",
                10,
                "lab_task:invalid-cohort",
                terminalization_version="v3",
            )
        await db.commit()

    async with factory() as db:
        issuer = await db.get(User, "invalid-cohort-issuer")
        holds = (await db.execute(select(CoinHold))).scalars().all()
        transactions = (await db.execute(select(Transaction))).scalars().all()
    assert issuer is not None and issuer.soul_coin_balance == 10
    assert holds == []
    assert transactions == []


@pytest.mark.anyio
@pytest.mark.parametrize("splits", INVALID_SPLITS)
async def test_invalid_settlement_splits_are_rejected_before_mutation(
    db_engine, splits
):
    factory = _factory(db_engine)
    hold_id = await _seed_hold(factory)
    before = await _finance_snapshot(factory, hold_id)

    async with factory() as db:
        with pytest.raises(coin_service.CoinError):
            await coin_service.settle(db, hold_id, splits)
        await db.rollback()

    assert await _finance_snapshot(factory, hold_id) == before


@pytest.mark.anyio
async def test_failure_after_first_credit_rolls_back_every_financial_effect(
    db_engine, monkeypatch
):
    factory = _factory(db_engine)
    hold_id = await _seed_hold(
        factory, amount=10, recipients=("creator_first", "creator_second")
    )
    before = await _finance_snapshot(factory, hold_id)
    real_reward = coin_service.reward_pending
    credited_recipients: list[str] = []

    async def credit_once_then_fail(db, user_id: str, amount: int, reason: str):
        if credited_recipients:
            raise InjectedSplitFailure("fault after the first distribution credit")
        credited_recipients.append(user_id)
        return await real_reward(db, user_id, amount, reason)

    monkeypatch.setattr(coin_service, "reward_pending", credit_once_then_fail)

    async with factory() as db:
        with pytest.raises(InjectedSplitFailure):
            await coin_service.settle(
                db,
                hold_id,
                [
                    ("creator_first", 4, "lab_reward:first"),
                    ("creator_second", 6, "lab_reward:second"),
                ],
            )
        await db.rollback()

    assert credited_recipients == ["creator_first"]
    assert await _finance_snapshot(factory, hold_id) == before


async def _seed_review_task(
    factory: async_sessionmaker[AsyncSession], *, status: str = "review"
) -> tuple[str, str]:
    async with factory() as db:
        db.add_all(
            [
                User(
                    id="terminal-issuer",
                    name="Terminal issuer",
                    email="terminal-issuer@finance.test",
                    soul_coin_balance=110,
                ),
                User(
                    id="terminal-creator",
                    name="Terminal creator",
                    email="terminal-creator@finance.test",
                    soul_coin_balance=0,
                ),
                Resident(
                    slug="terminal-researcher",
                    name="Terminal researcher",
                    creator_id="terminal-creator",
                    resident_type="npc",
                ),
            ]
        )
        await db.commit()

    async with factory() as db:
        hold_id = await coin_service.hold(
            db,
            "terminal-issuer",
            110,
            "lab_task:terminal-task",
            terminalization_version="v2",
        )
        assert hold_id is not None
        task = LabTask(
            id="terminal-task",
            issuer_user_id="terminal-issuer",
            researcher_slug="terminal-researcher",
            title="Atomic terminalization",
            brief_md="finance regression",
            scopes_json=["web_search"],
            reward_sc=100,
            platform_fee_sc=10,
            status=status,
            hold_id=hold_id,
            accepted_run_id="terminal-run",
        )
        now = datetime.now(UTC)
        db.add_all(
            [
                task,
                LabRun(
                    id="terminal-run",
                    task_id=task.id,
                    researcher_slug="terminal-researcher",
                    status="succeeded" if status in {"review", "rejected"} else "running",
                ),
                LabRunLease(
                    run_id="terminal-run",
                    owner_id="terminal-owner",
                    fencing_epoch=0,
                    heartbeat_at=now,
                    expires_at=now + timedelta(minutes=5),
                ),
            ]
        )
        await db.commit()
        return task.id, hold_id


async def _terminal_snapshot(
    factory: async_sessionmaker[AsyncSession], task_id: str, hold_id: str
) -> dict[str, object]:
    async with factory() as db:
        task = await db.get(LabTask, task_id)
        hold = await db.get(CoinHold, hold_id)
        assert task is not None and hold is not None
        balances = dict(
            (
                await db.execute(
                    select(User.id, User.soul_coin_balance).where(
                        User.id.in_(("terminal-issuer", "terminal-creator"))
                    )
                )
            ).all()
        )
        treasury = await db.get(ResidentTreasury, "terminal-researcher")
        transactions = (
            await db.execute(
                select(Transaction.user_id, Transaction.amount, Transaction.reason).where(
                    Transaction.user_id.in_(("terminal-issuer", "terminal-creator"))
                )
            )
        ).all()
        outbox = (await db.execute(select(OutboxEvent))).scalars().all()
        entries = (
            await db.execute(
                select(CoinHoldEntry).where(CoinHoldEntry.hold_id == hold_id)
            )
        ).scalars().all()
        commands = (
            await db.execute(
                select(LabTerminalizationCommand).where(
                    LabTerminalizationCommand.task_id == task_id
                )
            )
        ).scalars().all()
        receipts = (
            await db.execute(
                select(LabTerminalizationReceipt).where(
                    LabTerminalizationReceipt.task_id == task_id
                )
            )
        ).scalars().all()
        return {
            "task": (task.status, task.completed_at),
            "hold": (hold.status, hold.settled_at),
            "balances": balances,
            "treasury": None if treasury is None else treasury.balance_sc,
            "transactions": sorted(
                (str(user_id), amount, reason) for user_id, amount, reason in transactions
            ),
            "outbox": [
                {
                    "event_id": row.event_id,
                    "topic": row.topic,
                    "payload": row.payload_json,
                }
                for row in outbox
            ],
            "entries": sorted(
                (
                    row.terminal_action,
                    row.recipient_key,
                    row.amount,
                    row.operation_key,
                )
                for row in entries
            ),
            "commands": sorted(
                (row.command_id, row.operation, row.status) for row in commands
            ),
            "receipts": sorted(
                (row.receipt_id, row.command_id, row.event_id) for row in receipts
            ),
        }


async def _command_for_task(
    db: AsyncSession, task_id: str
) -> LabTerminalizationCommand:
    return (
        await db.execute(
            select(LabTerminalizationCommand).where(
                LabTerminalizationCommand.task_id == task_id
            )
        )
    ).scalar_one()


@pytest.mark.anyio
async def test_accept_terminalizes_task_hold_balances_and_outbox_together(
    db_engine, monkeypatch
):
    factory = _factory(db_engine)
    task_id, hold_id = await _seed_review_task(factory)
    monkeypatch.setattr(settings, "lab_creator_share", 0.2)
    monkeypatch.setattr(lab_task_service, "emit", AsyncMock())

    async with factory() as db:
        task = await lab_task_service.accept_result(db, task_id, "terminal-issuer")
        assert task.status == "review"
        command = await _command_for_task(db, task_id)
        assert command.status == "pending"
        await lab_terminalization_service.finalize(
            db,
            command.command_id,
            command.expected_epoch,
            _allow_local_kernel=True,
        )

    after = await _terminal_snapshot(factory, task_id, hold_id)
    assert after["task"][0] == "completed"
    assert after["task"][1] is not None
    assert after["hold"][0] == "settled"
    assert after["hold"][1] is not None
    assert after["balances"] == {
        "terminal-issuer": 0,
        "terminal-creator": 20,
    }
    assert after["treasury"] == 80
    assert [(action, recipient, amount) for action, recipient, amount, _ in after["entries"]] == [
        ("settle", "sink", 10),
        ("settle", "terminal-creator", 20),
        ("settle", "treasury:terminal-researcher", 80),
    ]
    assert len(after["receipts"]) == 1
    assert after["commands"][0][2] == "completed"

    terminal_outbox = after["outbox"]
    assert len(terminal_outbox) == 1
    assert terminal_outbox[0]["event_id"]
    assert task_id in json.dumps(terminal_outbox[0]["payload"], sort_keys=True)


@pytest.mark.anyio
async def test_terminal_commit_failure_rolls_back_task_hold_balances_and_outbox(
    db_engine, monkeypatch
):
    factory = _factory(db_engine)
    task_id, hold_id = await _seed_review_task(factory)
    before = await _terminal_snapshot(factory, task_id, hold_id)
    monkeypatch.setattr(settings, "lab_creator_share", 0.2)
    monkeypatch.setattr(lab_task_service, "emit", AsyncMock())
    fault_reached = False

    async with factory() as db:
        await lab_task_service.accept_result(db, task_id, "terminal-issuer")
        command = await _command_for_task(db, task_id)
        real_commit = db.commit

        async def fail_terminal_commit() -> None:
            nonlocal fault_reached
            task = next(
                (
                    value
                    for value in db.identity_map.values()
                    if isinstance(value, LabTask) and value.id == task_id
                ),
                None,
            )
            if task is not None and task.status == "completed":
                fault_reached = True
                raise InjectedTerminalCommitFailure("terminal commit fault")
            await real_commit()

        monkeypatch.setattr(db, "commit", fail_terminal_commit)
        with pytest.raises(InjectedTerminalCommitFailure):
            await lab_terminalization_service.finalize(
                db,
                command.command_id,
                command.expected_epoch,
                _allow_local_kernel=True,
            )

    assert fault_reached is True
    after = await _terminal_snapshot(factory, task_id, hold_id)
    for key in (
        "task",
        "hold",
        "balances",
        "treasury",
        "transactions",
        "outbox",
        "entries",
        "receipts",
    ):
        assert after[key] == before[key]
    assert after["commands"][0][2] == "pending"


@pytest.mark.anyio
async def test_refund_failure_cannot_leave_a_terminal_task_without_its_refund_outbox(
    db_engine, monkeypatch
):
    factory = _factory(db_engine)
    task_id, hold_id = await _seed_review_task(factory, status="assigned")
    before = await _terminal_snapshot(factory, task_id, hold_id)

    async def fail_refund_credit(*_args, **_kwargs):
        raise InjectedRefundFailure("refund credit fault")

    monkeypatch.setattr(coin_service, "reward_pending", fail_refund_credit)

    async with factory() as db:
        task = await lab_task_service.cancel_task(db, task_id, "terminal-issuer")
        assert task.status == "assigned"
        command = await _command_for_task(db, task_id)
        with pytest.raises(InjectedRefundFailure):
            await lab_terminalization_service.finalize(
                db,
                command.command_id,
                command.expected_epoch,
                _allow_local_kernel=True,
            )

    after = await _terminal_snapshot(factory, task_id, hold_id)
    for key in (
        "task",
        "hold",
        "balances",
        "treasury",
        "transactions",
        "outbox",
        "entries",
        "receipts",
    ):
        assert after[key] == before[key]
    assert after["commands"][0][2] == "pending"


@pytest.mark.anyio
async def test_failure_command_rejects_empty_runner_identity(db_engine):
    factory = _factory(db_engine)
    task_id, _ = await _seed_review_task(factory, status="assigned")

    async with factory() as db:
        task = await db.get(LabTask, task_id)
        assert task is not None
        for actor in (
            "runner:",
            "runner: ",
            "runner:worker 1",
            "runner:other-run",
        ):
            with pytest.raises(
                lab_terminalization_service.LabTerminalizationError,
                match="failure actor binding is invalid",
            ):
                await lab_terminalization_service.submit_command(
                    db,
                    task=task,
                    operation="fail",
                    actor=actor,
                )

        command = await lab_terminalization_service.submit_command(
            db,
            task=task,
            operation="fail",
            actor="runner:terminal-run",
        )

    assert command.actor == "runner:terminal-run"


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["review", "rejected"])
async def test_issuer_cannot_cancel_review_or_rejected_task(db_engine, status):
    factory = _factory(db_engine)
    task_id, hold_id = await _seed_review_task(factory, status=status)
    before = await _terminal_snapshot(factory, task_id, hold_id)

    async with factory() as db:
        with pytest.raises(lab_task_service.LabTaskError, match="not eligible"):
            await lab_task_service.cancel_task(db, task_id, "terminal-issuer")

    assert await _terminal_snapshot(factory, task_id, hold_id) == before


@pytest.mark.anyio
async def test_valid_sum_cannot_redirect_canonical_settlement(db_engine, monkeypatch):
    factory = _factory(db_engine)
    task_id, hold_id = await _seed_review_task(factory)
    monkeypatch.setattr(settings, "lab_creator_share", 0.2)

    async with factory() as db:
        await lab_task_service.accept_result(db, task_id, "terminal-issuer")
        command = await _command_for_task(db, task_id)
        payload = dict(command.payload_json)
        payload["splits"] = [dict(split) for split in payload["splits"]]
        payload["splits"][0]["recipient_key"] = "terminal-issuer"
        command.payload_json = payload
        await db.commit()

    before = await _terminal_snapshot(factory, task_id, hold_id)
    async with factory() as db:
        command = await _command_for_task(db, task_id)
        with pytest.raises(
            lab_terminalization_service.LabTerminalizationError,
            match="canonical settlement distribution",
        ):
            await lab_terminalization_service.finalize(
                db,
                command.command_id,
                command.expected_epoch,
                _allow_local_kernel=True,
            )

    assert await _terminal_snapshot(factory, task_id, hold_id) == before


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("decision", "operation"),
    [("settle", "arbitrate_settle"), ("refund", "arbitrate_refund")],
)
async def test_rejected_task_has_admin_only_arbitration_command(
    db_engine, decision, operation
):
    factory = _factory(db_engine)
    task_id, _ = await _seed_review_task(factory, status="rejected")
    async with factory() as db:
        db.add(
            User(
                id="terminal-admin",
                name="Terminal admin",
                email="terminal-admin@finance.test",
                soul_coin_balance=0,
                is_admin=True,
            )
        )
        await db.commit()

        task = await lab_task_service.arbitrate_result(
            db,
            task_id=task_id,
            admin_id="terminal-admin",
            decision=decision,
        )
        command = await _command_for_task(db, task_id)

    assert task.status == "rejected"
    assert command.operation == operation
    assert command.actor == "terminal-admin"
