"""E11 follow feed: follow/unfollow/cap, filtered feed, push, cursor, privacy after unfollow."""

from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.resident import Resident
from app.models.feed import Follow, FeedEvent


async def _user(db, email):
    u = User(name="u", email=email)
    db.add(u)
    await db.commit()
    return u


async def _resident(db, slug):
    r = Resident(slug=slug, name=slug, creator_id="system", district="cafe", status="idle", tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    return r


@pytest.mark.anyio
async def test_follow_unfollow(db_session):
    from app.services.feed_service import follow, unfollow
    user = await _user(db_session, "f@t.com")
    await _resident(db_session, "klaus")
    await follow(db_session, user.id, "klaus")
    await follow(db_session, user.id, "klaus")  # idempotent
    n = (await db_session.execute(select(Follow).where(Follow.user_id == user.id))).scalars().all()
    assert len(n) == 1
    await unfollow(db_session, user.id, "klaus")
    assert (await db_session.execute(select(Follow))).scalars().all() == []


@pytest.mark.anyio
async def test_follow_cap(db_session):
    from app.services.feed_service import follow, FeedError, FOLLOW_CAP
    user = await _user(db_session, "cap@t.com")
    for i in range(FOLLOW_CAP):
        db_session.add(Follow(user_id=user.id, resident_slug=f"r{i}"))
    await db_session.commit()
    await _resident(db_session, "extra")
    with pytest.raises(FeedError):
        await follow(db_session, user.id, "extra")


@pytest.mark.anyio
async def test_feed_only_followed(db_session):
    from app.services.feed_service import list_feed
    user = await _user(db_session, "feed@t.com")
    await _resident(db_session, "klaus")
    await _resident(db_session, "maria")
    db_session.add(Follow(user_id=user.id, resident_slug="klaus"))
    db_session.add(FeedEvent(resident_slug="klaus", kind="creation", payload_json={}))
    db_session.add(FeedEvent(resident_slug="maria", kind="creation", payload_json={}))  # not followed
    await db_session.commit()

    result = await list_feed(db_session, user.id)
    assert len(result["events"]) == 1 and result["events"][0]["resident_slug"] == "klaus"


@pytest.mark.anyio
async def test_push_writes_and_notifies_online_follower(db_session, db_engine):
    from app.services import feed_service as fs
    user = await _user(db_session, "push@t.com")
    await _resident(db_session, "klaus")
    db_session.add(Follow(user_id=user.id, resident_slug="klaus"))
    await db_session.commit()

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(fs, "async_session", factory), \
         patch.object(fs.manager, "is_online", AsyncMock(return_value=True)), \
         patch.object(fs.manager, "send", new_callable=AsyncMock) as send:
        await fs.push("klaus", "goal_achieved", {"title": "开咖啡馆"})

    events = (await db_session.execute(select(FeedEvent))).scalars().all()
    assert len(events) == 1 and events[0].kind == "goal_achieved"
    send.assert_awaited_once()
    assert send.call_args.args[1]["type"] == "feed_event"


@pytest.mark.anyio
async def test_feed_empty_after_unfollow(db_session):
    from app.services.feed_service import list_feed, unfollow
    user = await _user(db_session, "unf@t.com")
    await _resident(db_session, "klaus")
    db_session.add(Follow(user_id=user.id, resident_slug="klaus"))
    db_session.add(FeedEvent(resident_slug="klaus", kind="creation", payload_json={}))
    await db_session.commit()
    assert len((await list_feed(db_session, user.id))["events"]) == 1
    await unfollow(db_session, user.id, "klaus")
    assert (await list_feed(db_session, user.id))["events"] == []
