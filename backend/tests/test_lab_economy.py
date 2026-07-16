"""P1 — coin_service escrow + treasury (spec §4.4, §4.7, §6).

Covers the race-fixed atomic charge, transfer, hold/settle/refund with the
sum(splits)==hold.amount conservation invariant, and the atomic treasury
credit/debit. Pure coin_service on a single in-memory session.
"""
import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.transaction import Transaction
from app.models.coin_hold import CoinHold
from app.models.resident_treasury import ResidentTreasury
from app.services import coin_service


async def _mk_user(db, uid, balance):
    u = User(id=uid, name=uid, email=f"{uid}@test.com", soul_coin_balance=balance)
    db.add(u)
    await db.commit()
    return u


# ── charge race fix ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_charge_atomic_guard(db_session):
    await _mk_user(db_session, "u_a", 10)
    assert await coin_service.charge(db_session, "u_a", 10, "spend") is True
    assert await coin_service.get_balance(db_session, "u_a") == 0
    # Nothing left — the next charge must fail (WHERE balance>=amount guard).
    assert await coin_service.charge(db_session, "u_a", 1, "spend") is False
    assert await coin_service.get_balance(db_session, "u_a") == 0


@pytest.mark.anyio
async def test_charge_rejects_nonpositive(db_session):
    await _mk_user(db_session, "u_z", 10)
    assert await coin_service.charge(db_session, "u_z", 0, "noop") is False
    assert await coin_service.charge(db_session, "u_z", -5, "noop") is False
    assert await coin_service.get_balance(db_session, "u_z") == 10


# ── transfer ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_transfer_atomic(db_session):
    await _mk_user(db_session, "u_from", 30)
    await _mk_user(db_session, "u_to", 5)
    assert await coin_service.transfer(db_session, "u_from", "u_to", 20, "gift") is True
    assert await coin_service.get_balance(db_session, "u_from") == 10
    assert await coin_service.get_balance(db_session, "u_to") == 25
    # Insufficient → no movement.
    assert await coin_service.transfer(db_session, "u_from", "u_to", 999, "gift") is False
    assert await coin_service.get_balance(db_session, "u_from") == 10


# ── hold / settle / refund ────────────────────────────────────────────

@pytest.mark.anyio
async def test_hold_debits_and_records(db_session):
    await _mk_user(db_session, "issuer", 100)
    hold_id = await coin_service.hold(db_session, "issuer", 40, "lab_task:t1")
    assert hold_id is not None
    assert await coin_service.get_balance(db_session, "issuer") == 60
    h = await db_session.get(CoinHold, hold_id)
    assert h.status == "held" and h.amount == 40


@pytest.mark.anyio
async def test_hold_insufficient_returns_none(db_session):
    await _mk_user(db_session, "poor", 5)
    assert await coin_service.hold(db_session, "poor", 40, "lab_task:t2") is None
    assert await coin_service.get_balance(db_session, "poor") == 5


@pytest.mark.anyio
async def test_settle_conservation_and_treasury_sink(db_session):
    await _mk_user(db_session, "issuer", 100)
    await _mk_user(db_session, "creator", 0)
    hold_id = await coin_service.hold(db_session, "issuer", 50, "lab_task:t3")  # 40 reward + 10 fee

    # 40 reward split creator 10 / treasury 30, plus 10 fee → sink. Sum == 50.
    await coin_service.settle(db_session, hold_id, [
        ("creator", 10, "lab_reward:t3"),
        ("treasury:sage", 30, "lab_treasury:t3"),
        ("sink", 10, "lab_fee:t3"),
    ])
    assert await coin_service.get_balance(db_session, "creator") == 10
    assert await coin_service.treasury_balance(db_session, "sage") == 30
    h = await db_session.get(CoinHold, hold_id)
    assert h.status == "settled"


@pytest.mark.anyio
async def test_settle_rejects_mismatched_sum(db_session):
    await _mk_user(db_session, "issuer2", 100)
    hold_id = await coin_service.hold(db_session, "issuer2", 50, "lab_task:t4")
    with pytest.raises(coin_service.CoinError):
        await coin_service.settle(db_session, hold_id, [("issuer2", 49, "wrong")])
    # Hold stays held (not settled) so the money isn't lost.
    h = await db_session.get(CoinHold, hold_id)
    assert h.status == "held"


@pytest.mark.anyio
async def test_refund_returns_full(db_session):
    await _mk_user(db_session, "issuer3", 100)
    hold_id = await coin_service.hold(db_session, "issuer3", 50, "lab_task:t5")
    assert await coin_service.get_balance(db_session, "issuer3") == 50
    await coin_service.refund(db_session, hold_id, "lab_refund:t5")
    assert await coin_service.get_balance(db_session, "issuer3") == 100
    h = await db_session.get(CoinHold, hold_id)
    assert h.status == "refunded"


# ── treasury ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_treasury_credit_upsert_and_debit_guard(db_session):
    await coin_service.treasury_credit(db_session, "nova", 30, "seed")
    await coin_service.treasury_credit(db_session, "nova", 20, "more")  # upsert increment
    assert await coin_service.treasury_balance(db_session, "nova") == 50
    row = await db_session.get(ResidentTreasury, "nova")
    assert row.balance_sc == 50
    # Atomic debit: over-spend fails, exact spend works.
    assert await coin_service.treasury_debit(db_session, "nova", 999, "fuel") is False
    assert await coin_service.treasury_debit(db_session, "nova", 50, "fuel") is True
    assert await coin_service.treasury_balance(db_session, "nova") == 0
