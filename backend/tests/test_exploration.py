"""E8 exploration codex: first-visit lore, secret spot reward, codex API."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.location_visit import LocationVisit


@pytest.fixture
def lt_env(db_engine):
    from app.services import location_tracker as lt
    lt._last_location.clear()
    lt._secret_seen.clear()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(lt, "async_session", factory):
        yield lt
    lt._last_location.clear()
    lt._secret_seen.clear()


async def _user(db, email, bal=0):
    u = User(name="u", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


@pytest.mark.anyio
async def test_first_visit_notifies_lore(db_session, lt_env):
    lt = lt_env
    user = await _user(db_session, "expl@t.com")
    with patch("app.services.notification_service.manager.is_online", AsyncMock(return_value=False)):
        await lt.process_one(user.id, "academy")

    from app.models.notification import Notification
    notifs = (await db_session.execute(
        select(Notification).where(Notification.user_id == user.id)
    )).scalars().all()
    assert any("走廊" in n.body for n in notifs)  # academy lore delivered


@pytest.mark.anyio
async def test_secret_spot_rewards_once(db_session, lt_env):
    lt = lt_env
    user = await _user(db_session, "sec@t.com", bal=0)
    with patch("app.services.notification_service.manager.is_online", AsyncMock(return_value=False)):
        await lt.process_one(user.id, "academy:secret")
        await lt.process_one(user.id, "academy:secret")  # repeat = no extra reward

    await db_session.refresh(user)
    assert user.soul_coin_balance == 5  # rewarded once
    v = (await db_session.execute(
        select(LocationVisit).where(LocationVisit.location_id == "academy:secret")
    )).scalar_one()
    assert v.visit_count == 2


def test_on_move_detects_secret_tile():
    from app.services import location_tracker as lt
    lt._last_location.clear(); lt._secret_seen.clear()
    while not lt._queue.empty():
        lt._queue.get_nowait()
    # academy secret tile is (17,20)
    lt.on_move("u1", 17, 20)
    queued = []
    while not lt._queue.empty():
        queued.append(lt._queue.get_nowait())
    assert ("u1", "academy:secret") in queued
    lt._secret_seen.clear()


def test_lore_covers_poll_built_locations():
    """S8:邮局与剧院是公投建出来的两座楼(civic_service.CIVIC_AGENDA),但 LORE
    里只有最初的 8 个地点 —— 图鉴翻到它们是空词条。补上之后首访通知才有话说。"""
    from app.agent.location_lore import LORE, lore_for
    assert {"post_office", "theater"} <= set(LORE)
    assert lore_for("post_office") == LORE["post_office"]
    assert lore_for("theater") == LORE["theater"]


@pytest.mark.anyio
async def test_codex_api(client, db_session):
    from app.services.auth_service import create_token
    user = await _user(db_session, "codex@t.com")
    db_session.add(LocationVisit(user_id=user.id, location_id="academy", visit_count=3))
    db_session.add(LocationVisit(user_id=user.id, location_id="academy:secret", visit_count=1))
    await db_session.commit()

    resp = await client.get("/exploration/me", headers={"Authorization": f"Bearer {create_token(user.id)}"})
    assert resp.status_code == 200
    body = resp.json()
    academy = next(e for e in body["locations"] if e["location_id"] == "academy")
    assert academy["visited"] and academy["secret_found"] and academy["visit_count"] == 3
    assert body["visited"] >= 1
    # Every entry carries a tile-space rect for the codex minimap silhouette.
    assert len(academy["bounds"]) == 4
    assert all(len(e["bounds"]) == 4 for e in body["locations"])
