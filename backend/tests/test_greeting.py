"""A3 greeting_service: proactive resident greetings on connect."""

from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.resident import Resident
from app.models.memory import Memory


@pytest.fixture
def greet_session(db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.services.greeting_service.async_session", factory):
        yield


async def _user(db, email):
    u = User(name="玩家", email=email, soul_coin_balance=0)
    db.add(u)
    await db.commit()
    return u


async def _resident(db, slug, status="idle", heat=10):
    r = Resident(slug=slug, name="克劳斯", creator_id="system",
                 district="central_plaza", status=status, heat=heat, tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    return r


async def _relationship(db, resident_id, user_id, importance=0.9):
    m = Memory(resident_id=resident_id, type="relationship", content="老朋友",
               importance=importance, source="relationship", related_user_id=user_id)
    db.add(m)
    await db.commit()
    return m


@pytest.mark.anyio
async def test_no_relationship_no_greeting(db_session, greet_session):
    from app.services import greeting_service as gs
    user = await _user(db_session, "new@a3.com")
    await _resident(db_session, "klaus")

    with patch.object(gs.manager, "is_online", AsyncMock(return_value=True)), \
         patch.object(gs.manager, "send", new_callable=AsyncMock) as send:
        await gs.maybe_greet(user.id)

    send.assert_not_awaited()


@pytest.mark.anyio
async def test_strong_relationship_greets_and_records(db_session, greet_session):
    from app.services import greeting_service as gs
    user = await _user(db_session, "old@a3.com")
    res = await _resident(db_session, "klaus", heat=40)
    await _relationship(db_session, res.id, user.id, importance=0.9)

    with patch.object(gs.manager, "is_online", AsyncMock(return_value=True)), \
         patch.object(gs.manager, "send", new_callable=AsyncMock) as send:
        await gs.maybe_greet(user.id)

    greetings = [c for c in send.call_args_list if c.args[1].get("type") == "resident_greeting"]
    assert len(greetings) == 1
    assert greetings[0].args[1]["resident_slug"] == "klaus"

    mem = (await db_session.execute(
        select(Memory).where(Memory.source == "greeting", Memory.related_user_id == user.id)
    )).scalars().all()
    assert len(mem) == 1


@pytest.mark.anyio
async def test_24h_cooldown_skips(db_session, greet_session):
    from app.services import greeting_service as gs
    user = await _user(db_session, "cd@a3.com")
    res = await _resident(db_session, "klaus")
    await _relationship(db_session, res.id, user.id, importance=0.9)
    # Pre-existing greeting today.
    db_session.add(Memory(resident_id=res.id, type="event", content="打招呼",
                          importance=0.2, source="greeting", related_user_id=user.id,
                          created_at=datetime.now(UTC) - timedelta(hours=2)))
    await db_session.commit()

    with patch.object(gs.manager, "is_online", AsyncMock(return_value=True)), \
         patch.object(gs.manager, "send", new_callable=AsyncMock) as send:
        await gs.maybe_greet(user.id)

    assert not [c for c in send.call_args_list if c.args[1].get("type") == "resident_greeting"]


@pytest.mark.anyio
async def test_non_idle_resident_skipped(db_session, greet_session):
    from app.services import greeting_service as gs
    user = await _user(db_session, "busy@a3.com")
    res = await _resident(db_session, "klaus", status="chatting")
    await _relationship(db_session, res.id, user.id, importance=0.9)

    with patch.object(gs.manager, "is_online", AsyncMock(return_value=True)), \
         patch.object(gs.manager, "send", new_callable=AsyncMock) as send:
        await gs.maybe_greet(user.id)

    assert not [c for c in send.call_args_list if c.args[1].get("type") == "resident_greeting"]


@pytest.mark.anyio
async def test_close_friend_may_receive_gift(db_session, greet_session):
    from app.services import greeting_service as gs
    from app.services.shop_service import seed_items
    await seed_items(db_session)  # provides gift items
    user = await _user(db_session, "bff@a3.com")
    res = await _resident(db_session, "klaus", heat=50)
    await _relationship(db_session, res.id, user.id, importance=0.95)

    with patch.object(gs.manager, "is_online", AsyncMock(return_value=True)), \
         patch.object(gs.manager, "send", new_callable=AsyncMock) as send:
        await gs.maybe_greet(user.id)

    greeting = [c for c in send.call_args_list if c.args[1].get("type") == "resident_greeting"][0]
    assert greeting.args[1]["gift"] is not None
    assert "code" in greeting.args[1]["gift"]
