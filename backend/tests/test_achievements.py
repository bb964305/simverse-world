"""S2 event bus + achievement engine."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.achievement import UserAchievement


@pytest.fixture
def ach_session(db_engine):
    """Point the achievement engine's own-session writes at the test DB."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.events.achievements.async_session", factory):
        yield


# ── bus ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_bus_emit_runs_and_isolates_failures():
    from app.events import bus

    calls = []

    @bus.on("t_evt")
    async def _ok(db, **kw):
        calls.append("ok")

    @bus.on("t_evt")
    async def _boom(db, **kw):
        raise RuntimeError("nope")

    @bus.on("t_evt")
    async def _also(db, **kw):
        calls.append("also")

    await bus.emit(None, "t_evt", x=1)  # must not raise
    assert calls == ["ok", "also"]


# ── unlock / counting ────────────────────────────────────────────────

async def _mk_user(db_session, email):
    user = User(name="a", email=email, soul_coin_balance=0)
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.mark.anyio
async def test_first_chat_unlocks_and_rewards(db_session, ach_session):
    from app.events.bus import emit
    import app.events.achievements as ach

    user = await _mk_user(db_session, "fc@test.com")
    with patch.object(ach.manager, "send", new_callable=AsyncMock) as send:
        await emit(None, "chat_completed", user_id=user.id, resident_id="r", turns=3)

    ua = (await db_session.execute(
        select(UserAchievement).where(UserAchievement.user_id == user.id,
                                      UserAchievement.code == "first_chat")
    )).scalar_one()
    assert ua.unlocked_at is not None

    await db_session.refresh(user)
    assert user.soul_coin_balance == 20  # first_chat reward
    assert any(c.args[1]["type"] == "achievement_unlocked" for c in send.call_args_list)


@pytest.mark.anyio
async def test_unlock_is_idempotent(db_session, ach_session):
    from app.events.achievements import unlock

    user = await _mk_user(db_session, "idem@test.com")
    with patch("app.events.achievements.manager.send", new_callable=AsyncMock):
        first = await unlock(user.id, "first_chat")
        second = await unlock(user.id, "first_chat")

    assert first == "first_chat"
    assert second is None
    await db_session.refresh(user)
    assert user.soul_coin_balance == 20  # rewarded once, not twice


@pytest.mark.anyio
async def test_counting_unlocks_at_target(db_session, ach_session):
    from app.events.achievements import increment

    user = await _mk_user(db_session, "count@test.com")
    with patch("app.events.achievements.manager.send", new_callable=AsyncMock):
        for i in range(9):
            assert await increment(user.id, "memory_keeper_10", 10) is None
        # 10th increment unlocks
        assert await increment(user.id, "memory_keeper_10", 10) == "memory_keeper_10"

    ua = (await db_session.execute(
        select(UserAchievement).where(UserAchievement.code == "memory_keeper_10")
    )).scalar_one()
    assert ua.unlocked_at is not None
    assert ua.progress_json["count"] == 10


@pytest.mark.anyio
async def test_get_user_achievements_merges_progress(db_session, ach_session):
    from app.events.achievements import unlock, get_user_achievements

    user = await _mk_user(db_session, "merge@test.com")
    with patch("app.events.achievements.manager.send", new_callable=AsyncMock):
        await unlock(user.id, "first_chat")

    merged = await get_user_achievements(db_session, user.id)
    by_code = {m["code"]: m for m in merged}
    assert by_code["first_chat"]["unlocked"] is True
    assert by_code["memory_keeper_10"]["unlocked"] is False
    # every definition is represented
    assert len(merged) == 12


# ── D1: one case per achievement (fake events → unlock) ──────────────

D1_CASES = [
    ("first_chat", [("chat_completed", {"resident_id": "r1", "turns": 1})]),
    ("deep_talk", [("chat_completed", {"resident_id": "r1", "turns": 10})]),
    ("remembered", [("memory_written_about_user", {})]),
    ("memory_keeper_10", [("memory_written_about_user", {})] * 10),
    ("soul_shaper", [("personality_shifted", {})]),
    ("week_streak", [("login_streak", {"streak": 7})]),
    ("explorer_5", [("location_first_visit", {})] * 5),
    ("explorer_all", [("location_first_visit", {})] * 20),
    ("errand_runner", [("commission_completed", {})]),
    ("patron", [("purchase_tip", {})]),
    ("socialite", [("chat_completed", {"resident_id": f"r{i}", "turns": 1}) for i in range(10)]),
    ("dreamt_of", [("dream_generated", {})]),
]


@pytest.mark.anyio
@pytest.mark.parametrize("code,emits", D1_CASES, ids=[c[0] for c in D1_CASES])
async def test_each_achievement_unlocks(db_session, ach_session, code, emits):
    from app.events.bus import emit
    import app.events.achievements as ach

    user = await _mk_user(db_session, f"{code}@d1.com")
    with patch.object(ach.manager, "send", new_callable=AsyncMock):
        for event, kw in emits:
            await emit(None, event, user_id=user.id, **kw)

    ua = (await db_session.execute(
        select(UserAchievement).where(
            UserAchievement.user_id == user.id, UserAchievement.code == code,
        )
    )).scalar_one_or_none()
    assert ua is not None and ua.unlocked_at is not None, f"{code} should be unlocked"


@pytest.mark.anyio
async def test_achievements_api(client, db_session, ach_session):
    from app.services.auth_service import create_token

    user = await _mk_user(db_session, "api@test.com")
    headers = {"Authorization": f"Bearer {create_token(user.id)}"}
    resp = await client.get("/achievements", headers=headers)
    assert resp.status_code == 200
    codes = {a["code"] for a in resp.json()["achievements"]}
    assert "first_chat" in codes
