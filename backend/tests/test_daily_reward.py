import pytest
from app.models.user import User
from datetime import datetime, timedelta, UTC
from app.services.daily_reward_service import claim_daily_reward

@pytest.fixture
async def test_user(db_session):
    user = User(id="daily-test-user", name="DailyUser",
                email="daily@test.com", soul_coin_balance=100)
    db_session.add(user)
    await db_session.commit()
    return user

@pytest.mark.anyio
async def test_user_has_last_daily_reward_field(db_session, test_user):
    assert hasattr(test_user, "last_daily_reward_at")
    assert test_user.last_daily_reward_at is None

@pytest.mark.anyio
async def test_resident_has_search_vector_field(db_session):
    from app.models.resident import Resident
    r = Resident.__table__.columns
    assert "search_vector" in [c.name for c in r]

@pytest.mark.anyio
async def test_claim_daily_reward_first_time(db_session, test_user):
    initial_balance = test_user.soul_coin_balance
    result = await claim_daily_reward(db_session, test_user.id)
    assert result["claimed"] is True
    assert result["amount"] == 10 and result["streak"] == 1  # ladder day 1
    assert result["new_balance"] == initial_balance + 10

@pytest.mark.anyio
async def test_claim_daily_reward_already_claimed_today(db_session, test_user):
    await claim_daily_reward(db_session, test_user.id)
    result = await claim_daily_reward(db_session, test_user.id)
    assert result["claimed"] is False
    assert result["reason"] == "already_claimed_today"

@pytest.mark.anyio
async def test_consecutive_login_advances_streak(db_session, test_user):
    from datetime import datetime, UTC
    test_user.last_login_date = datetime.now(UTC).date() - timedelta(days=1)
    test_user.login_streak = 1
    await db_session.commit()
    result = await claim_daily_reward(db_session, test_user.id)
    assert result["claimed"] is True and result["streak"] == 2 and result["amount"] == 15

@pytest.mark.anyio
async def test_broken_streak_resets_to_one(db_session, test_user):
    from datetime import datetime, UTC
    test_user.last_login_date = datetime.now(UTC).date() - timedelta(days=3)  # gap
    test_user.login_streak = 5
    await db_session.commit()
    result = await claim_daily_reward(db_session, test_user.id)
    assert result["streak"] == 1 and result["amount"] == 10

@pytest.mark.anyio
async def test_streak_caps_at_day_7(db_session, test_user):
    from datetime import datetime, UTC
    test_user.last_login_date = datetime.now(UTC).date() - timedelta(days=1)
    test_user.login_streak = 9  # already past the cap
    await db_session.commit()
    result = await claim_daily_reward(db_session, test_user.id)
    assert result["streak"] == 10 and result["amount"] == 50  # capped ladder value
