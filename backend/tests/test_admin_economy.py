import pytest
from app.models.user import User
from app.models.transaction import Transaction


@pytest.mark.anyio
async def test_economy_global_stats(db_session):
    """Economy stats should aggregate correctly."""
    from app.routers.admin.economy import _get_economy_stats

    u1 = User(name="u1", email="u1@test.com", soul_coin_balance=150,
              is_admin=False, is_banned=False)
    u2 = User(name="u2", email="u2@test.com", soul_coin_balance=50,
              is_admin=False, is_banned=False)
    db_session.add_all([u1, u2])
    await db_session.commit()

    # Positive transactions (issued)
    db_session.add(Transaction(user_id=u1.id, amount=100, reason="signup"))
    db_session.add(Transaction(user_id=u2.id, amount=100, reason="signup"))
    db_session.add(Transaction(user_id=u1.id, amount=50, reason="daily"))
    # Negative transactions (consumed)
    db_session.add(Transaction(user_id=u2.id, amount=-50, reason="chat"))
    await db_session.commit()

    stats = await _get_economy_stats(db_session)
    assert stats["total_issued"] == 250  # 100 + 100 + 50
    assert stats["total_consumed"] == 50  # abs(-50)
    assert stats["net_circulation"] == 200  # 250 - 50
    assert stats["total_users"] == 2
    assert stats["avg_balance"] == 100.0  # (150 + 50) / 2


@pytest.mark.anyio
async def test_economy_transaction_log(db_session):
    """Transaction log should support pagination and filters."""
    from app.routers.admin.economy import _get_transaction_log

    u = User(name="txn", email="txn@test.com", is_admin=False, is_banned=False)
    db_session.add(u)
    await db_session.commit()

    for i in range(8):
        db_session.add(Transaction(
            user_id=u.id,
            amount=10 if i % 2 == 0 else -5,
            reason="signup" if i % 2 == 0 else "chat",
        ))
    await db_session.commit()

    txns, total = await _get_transaction_log(db_session, offset=0, limit=5)
    assert total == 8
    assert len(txns) == 5

    # Filter by reason
    txns2, total2 = await _get_transaction_log(db_session, reason="chat")
    assert total2 == 4


@pytest.mark.anyio
async def test_economy_config_update(db_session):
    """Economy config update should write to ConfigService."""
    from app.routers.admin.economy import _update_economy_config
    from app.services.config_service import ConfigService

    svc = ConfigService(db_session)
    await _update_economy_config(db_session, admin_id="admin-1", signup_bonus=200, daily_reward=10)

    value = await svc.get("economy.signup_bonus", default=100)
    assert value == 200

    value2 = await svc.get("economy.daily_reward", default=5)
    assert value2 == 10


@pytest.mark.anyio
async def test_economy_series_daily_buckets(client, db_session):
    """/admin/economy/series zero-fills days and splits issued/consumed."""
    from datetime import datetime, timedelta, UTC
    from app.services.auth_service import create_token

    admin = User(name="adm", email="adm-series@test.com", is_admin=True, is_banned=False)
    u = User(name="s1", email="s1@test.com", is_admin=False, is_banned=False)
    db_session.add_all([admin, u])
    await db_session.commit()

    now = datetime.now(UTC)
    db_session.add(Transaction(user_id=u.id, amount=100, reason="signup", created_at=now))
    db_session.add(Transaction(user_id=u.id, amount=-30, reason="chat", created_at=now))
    db_session.add(Transaction(user_id=u.id, amount=50, reason="daily",
                               created_at=now - timedelta(days=2)))
    await db_session.commit()

    headers = {"Authorization": f"Bearer {create_token(admin.id)}"}
    resp = await client.get("/admin/economy/series?days=7", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 7
    assert len(body["series"]) == 7  # zero-filled, stable x-axis
    today = body["series"][-1]
    assert today["issued"] == 100 and today["consumed"] == 30 and today["net"] == 70
    two_ago = body["series"][-3]
    assert two_ago["issued"] == 50 and two_ago["consumed"] == 0
    assert all(p["issued"] == 0 and p["consumed"] == 0 for p in body["series"][:4])
