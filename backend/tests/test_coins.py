import pytest
from app.models.user import User
from app.services.coin_service import get_balance, charge, reward


@pytest.fixture
async def test_user(db_session):
    user = User(id="coin-test-user", name="CoinUser", email="coins@test.com", soul_coin_balance=100)
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.mark.anyio
async def test_get_balance(db_session, test_user):
    balance = await get_balance(db_session, test_user.id)
    assert balance == 100


@pytest.mark.anyio
async def test_charge_deducts_balance(db_session, test_user):
    ok = await charge(db_session, test_user.id, 5, "test_charge")
    assert ok is True
    balance = await get_balance(db_session, test_user.id)
    assert balance == 95


@pytest.mark.anyio
async def test_charge_fails_if_insufficient(db_session, test_user):
    ok = await charge(db_session, test_user.id, 200, "too_much")
    assert ok is False
    balance = await get_balance(db_session, test_user.id)
    assert balance == 100  # unchanged


@pytest.mark.anyio
async def test_reward_adds_balance(db_session, test_user):
    new_balance = await reward(db_session, test_user.id, 50, "test_reward")
    assert new_balance == 150


@pytest.mark.anyio
async def test_charge_records_transaction(db_session, test_user):
    from sqlalchemy import select
    from app.models.transaction import Transaction
    await charge(db_session, test_user.id, 1, "chat:isabella")
    txns = await db_session.execute(
        select(Transaction).where(Transaction.user_id == test_user.id)
    )
    txn_list = txns.scalars().all()
    assert len(txn_list) == 1
    assert txn_list[0].amount == -1
    assert txn_list[0].reason == "chat:isabella"


@pytest.mark.anyio
async def test_hold_failure_leaves_caller_orm_object_usable(db_session, test_user):
    """Regression: an insufficient hold() must NOT expire the caller's ORM objects.

    The atomic debit UPDATE matches 0 rows and writes nothing, so hold() must not
    rollback — a rollback expires every object in the session (independent of
    expire_on_commit), and the next lazy attribute access then raises
    MissingGreenlet under asyncio (e.g. the freshly-created LabTask that
    lab_task_service mutates right after a failed hold)."""
    from app.services.coin_service import hold
    hold_id = await hold(db_session, test_user.id, 500, "too_much")
    assert hold_id is None
    # Would raise sqlalchemy.exc.MissingGreenlet if the object were expired:
    assert test_user.soul_coin_balance == 100


@pytest.mark.anyio
async def test_transfer_failure_leaves_caller_orm_object_usable(db_session, test_user):
    """Regression: an insufficient transfer() (debit guard fails) must not expire
    the caller's ORM objects — same MissingGreenlet hazard as charge()/hold()."""
    from app.services.coin_service import transfer
    recipient = User(id="coin-test-recipient", name="Recip", email="recip@test.com", soul_coin_balance=0)
    db_session.add(recipient)
    await db_session.commit()
    ok = await transfer(db_session, test_user.id, recipient.id, 500, "too_much")
    assert ok is False
    assert test_user.soul_coin_balance == 100
