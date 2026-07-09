"""D3 daily topic quest: generation (idempotent), completion via chat_completed, API."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.resident import Resident
from app.models.daily_quest import DailyQuest


async def _user(db, email):
    u = User(name="p", email=email, soul_coin_balance=0)
    db.add(u)
    await db.commit()
    return u


async def _resident(db, slug):
    r = Resident(slug=slug, name="克劳斯", creator_id="system",
                 district="central_plaza", status="idle", heat=5, tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    return r


@pytest.mark.anyio
async def test_generate_quest_idempotent(db_session):
    from app.services.daily_quest_service import generate_daily_quest
    user = await _user(db_session, "dq@test.com")
    await _resident(db_session, "klaus")

    q1 = await generate_daily_quest(db_session, user.id)
    q2 = await generate_daily_quest(db_session, user.id)
    assert q1 is not None and q1.id == q2.id
    assert q1.quest_json["resident_slug"] == "klaus" and q1.status == "pending"
    n = (await db_session.execute(select(func.count()).select_from(DailyQuest))).scalar()
    assert n == 1


@pytest.mark.anyio
async def test_quest_completes_on_matching_chat(db_session):
    from app.services.daily_quest_service import generate_daily_quest
    from app.events.bus import emit

    user = await _user(db_session, "dq2@test.com")
    res = await _resident(db_session, "klaus")
    quest = await generate_daily_quest(db_session, user.id)
    assert quest.quest_json["resident_slug"] == "klaus"

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    with patch("app.services.daily_quest_service.async_session", factory), \
         patch("app.services.notification_service.manager.is_online", AsyncMock(return_value=False)):
        # too few turns → not done
        await emit(None, "chat_completed", user_id=user.id, resident_id=res.id, turns=1)
        await db_session.refresh(quest)
        assert quest.status == "pending"
        # enough turns, matching resident → done + reward
        await emit(None, "chat_completed", user_id=user.id, resident_id=res.id, turns=3)
        await db_session.refresh(quest)
        assert quest.status == "done"

    await db_session.refresh(user)
    assert user.soul_coin_balance == quest.reward_sc


@pytest.mark.anyio
async def test_quest_ignores_wrong_resident(db_session):
    from app.services.daily_quest_service import generate_daily_quest
    from app.events.bus import emit

    user = await _user(db_session, "dq3@test.com")
    await _resident(db_session, "klaus")
    other = await _resident(db_session, "maria")
    quest = await generate_daily_quest(db_session, user.id)
    # ensure quest targets klaus (heat asc order + bias picks klaus/maria; force it)
    quest.quest_json = {**quest.quest_json, "resident_slug": "klaus"}
    await db_session.commit()

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    with patch("app.services.daily_quest_service.async_session", factory):
        await emit(None, "chat_completed", user_id=user.id, resident_id=other.id, turns=5)
    await db_session.refresh(quest)
    assert quest.status == "pending"


@pytest.mark.anyio
async def test_daily_quest_api(client, db_session):
    from app.services.daily_quest_service import generate_daily_quest
    from app.services.auth_service import create_token

    user = await _user(db_session, "dq4@test.com")
    await _resident(db_session, "klaus")
    await generate_daily_quest(db_session, user.id)

    resp = await client.get("/daily/quest", headers={"Authorization": f"Bearer {create_token(user.id)}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["quest"]["status"] == "pending"
    assert "login_streak" in body
