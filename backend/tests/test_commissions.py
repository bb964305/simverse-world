"""B1 commissions: create/cap, optimistic accept, completion (deliver/chat/visit), expiry."""

from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.resident import Resident
from app.models.commission import Commission
from app.models.conversation import Conversation, Message


async def _user(db, email, bal=0):
    u = User(name="p", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


async def _resident(db, slug):
    r = Resident(slug=slug, name=slug, creator_id="system",
                 district="central_plaza", status="idle", tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    return r


async def _commission(db, issuer_id, kind, payload, reward=20, expires_in_h=48):
    from app.services.commission_service import create_commission
    c = await create_commission(db, issuer_id, kind, "带个话", payload, reward)
    if expires_in_h != 48:
        c.expires_at = datetime.now(UTC) + timedelta(hours=expires_in_h)
        await db.commit()
    return c


@pytest.mark.anyio
async def test_optimistic_accept_only_one_wins(db_session):
    from app.services.commission_service import accept, CommissionError
    boss = await _resident(db_session, "boss")
    a = await _user(db_session, "a@c.com")
    b = await _user(db_session, "b@c.com")
    c = await _commission(db_session, boss.id, "chat_topic", {"target_slug": "boss", "min_turns": 2})

    got = await accept(db_session, c.id, a.id)
    assert got.status == "accepted" and got.acceptor_user_id == a.id
    with pytest.raises(CommissionError):
        await accept(db_session, c.id, b.id)  # already taken


@pytest.mark.anyio
async def test_accept_expired_fails(db_session):
    from app.services.commission_service import accept, CommissionError
    boss = await _resident(db_session, "boss")
    u = await _user(db_session, "e@c.com")
    c = await _commission(db_session, boss.id, "visit_location", {"location_id": "academy"}, expires_in_h=-1)
    with pytest.raises(CommissionError):
        await accept(db_session, c.id, u.id)


@pytest.mark.anyio
async def test_global_cap(db_session):
    from app.services.commission_service import create_commission, _cap
    boss = await _resident(db_session, "boss")
    for i in range(_cap()):
        await create_commission(db_session, boss.id, f"kind_{i}", "带话", {"target_slug": "boss"}, 10)
    overflow = await create_commission(db_session, boss.id, "kind_overflow", "带话", {"target_slug": "boss"}, 10)
    assert overflow is None


@pytest.mark.anyio
async def test_create_uses_configured_ttl_and_deduplicates_active_kind(
    db_session, monkeypatch,
):
    from app.config import settings
    from app.services.commission_service import create_commission

    boss = await _resident(db_session, "dedupe-boss")
    monkeypatch.setattr(settings, "commission_lifecycle_v2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "commission_ttl_hours", 72)
    before = datetime.now(UTC)
    first = await create_commission(
        db_session, boss.id, "visit_location", "取件",
        {"location_id": "workshop"}, 8,
    )
    after = datetime.now(UTC)
    duplicate = await create_commission(
        db_session, boss.id, "visit_location", "重复取件",
        {"location_id": "workshop"}, 8,
    )

    assert isinstance(first, Commission)
    # SQLite drops timezone metadata from DateTime(timezone=True).
    expiry = first.expires_at
    if expiry.tzinfo is None:
        before, after = before.replace(tzinfo=None), after.replace(tzinfo=None)
    assert before + timedelta(hours=72) <= expiry <= (
        after + timedelta(hours=72)
    )
    assert duplicate == "deduped"

    first.status = "completed"
    await db_session.commit()
    assert isinstance(await create_commission(
        db_session, boss.id, "visit_location", "下一单",
        {"location_id": "workshop"}, 8,
    ), Commission)


@pytest.mark.anyio
async def test_default_flag_off_no_dedup_and_48h_ttl(db_session, monkeypatch):
    from app.config import Settings, settings
    from app.services.commission_service import create_commission

    # Regression anchor: the v2 gate ships dark.
    assert Settings(_env_file=None).commission_lifecycle_v2_enabled is False
    monkeypatch.setattr(settings, "commission_lifecycle_v2_enabled", False, raising=False)

    boss = await _resident(db_session, "flagoff-boss")
    before = datetime.now(UTC)
    first = await create_commission(
        db_session, boss.id, "visit_location", "取件",
        {"location_id": "workshop"}, 8,
    )
    second = await create_commission(
        db_session, boss.id, "visit_location", "重复取件",
        {"location_id": "workshop"}, 8,
    )
    after = datetime.now(UTC)

    # master semantics: no dedup — same issuer+kind posts twice just fine.
    assert isinstance(first, Commission)
    assert isinstance(second, Commission)
    # master TTL: model default 48h, settings TTL not applied while the gate is off.
    expiry = first.expires_at
    if expiry.tzinfo is None:
        before, after = before.replace(tzinfo=None), after.replace(tzinfo=None)
    assert before + timedelta(hours=48) <= expiry <= (
        after + timedelta(hours=48)
    )


@pytest.mark.anyio
async def test_dedup_ignores_expired_open_commissions(db_session, monkeypatch):
    from app.config import settings
    from app.services.commission_service import create_commission

    monkeypatch.setattr(settings, "commission_lifecycle_v2_enabled", True, raising=False)

    boss = await _resident(db_session, "expired-boss")
    stale = await create_commission(
        db_session, boss.id, "visit_location", "旧取件",
        {"location_id": "workshop"}, 8,
    )
    assert isinstance(stale, Commission)
    stale.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()

    # An open-but-expired commission must not block a fresh one.
    fresh = await create_commission(
        db_session, boss.id, "visit_location", "新取件",
        {"location_id": "workshop"}, 8,
    )
    assert isinstance(fresh, Commission)


@pytest.mark.anyio
async def test_work_pays_wage_when_deduped(db_session, monkeypatch):
    from app.config import settings
    from app.redis_client import get_redis
    from app.services import duty_service
    from app.services.commission_service import create_commission

    monkeypatch.setattr(settings, "commission_lifecycle_v2_enabled", True, raising=False)

    fixer = Resident(
        slug="fixer-chen", name="陈铁生", creator_id="system",
        district="central_plaza", status="idle", tile_x=1, tile_y=1,
        meta_json={"duty": {"key": "workshop_fixer",
                            "perks": {"commission_reward": 8}}},
    )
    db_session.add(fixer)
    await db_session.commit()

    # Pre-existing active errand of the same issuer+kind → next WORK dedups.
    first = await create_commission(
        db_session, fixer.id, "visit_location", "取件",
        {"location_id": "workshop"}, 8,
    )
    assert isinstance(first, Commission)

    pay_calls = []

    async def _fake_pay(db, resident):
        pay_calls.append(resident.slug)

    list_calls = []

    async def _fake_list(db, resident, *a, **kw):
        list_calls.append(resident.slug)

    monkeypatch.setattr(duty_service, "_pay_wage", _fake_pay)
    monkeypatch.setattr(duty_service, "_maybe_list_resident_work", _fake_list)

    line = await duty_service.on_work(db_session, fixer)

    # Dedup hit still counts as a worked shift: narrative line (not None) so
    # on_work pays the wage and sets the cooldown.
    assert isinstance(line, str) and line
    assert pay_calls == [fixer.slug]
    assert list_calls == []  # no new shop item on the dedup path
    assert await get_redis().exists(f"sv:duty_work:{fixer.id}")
    rows = (await db_session.execute(select(Commission))).scalars().all()
    assert len(rows) == 1  # no second commission created


@pytest.mark.anyio
async def test_deliver_completion_full_chain(db_session):
    from app.services.commission_service import accept
    from app.events.bus import emit

    boss = await _resident(db_session, "boss")
    azhen = await _resident(db_session, "azhen")
    user = await _user(db_session, "d@c.com", bal=0)
    c = await _commission(db_session, boss.id, "deliver_message",
                          {"target_slug": "azhen", "message": "告诉阿珍 我很想你"}, reward=30)
    await accept(db_session, c.id, user.id)

    conv = Conversation(resident_id=azhen.id, user_id=user.id)
    db_session.add(conv)
    await db_session.commit()
    db_session.add(Message(conversation_id=conv.id, role="user", content="告诉阿珍 我很想你 记得回来"))
    await db_session.commit()

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    with patch("app.services.commission_service.async_session", factory), \
         patch("app.services.notification_service.manager.is_online", AsyncMock(return_value=False)):
        await emit(None, "chat_completed", user_id=user.id, resident_id=azhen.id, turns=5, conversation_id=conv.id)

    await db_session.refresh(c)
    assert c.status == "completed"
    await db_session.refresh(user)
    assert user.soul_coin_balance == 30
    mems = (await db_session.execute(select(Message))).scalars().all()  # sanity: conv message exists
    assert mems


@pytest.mark.anyio
async def test_chat_topic_completion_requires_min_turns(db_session):
    from app.services.commission_service import accept
    from app.events.bus import emit

    boss = await _resident(db_session, "boss")
    user = await _user(db_session, "ct@c.com")
    c = await _commission(db_session, boss.id, "chat_topic", {"target_slug": "boss", "min_turns": 4})
    await accept(db_session, c.id, user.id)

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    with patch("app.services.commission_service.async_session", factory), \
         patch("app.services.notification_service.manager.is_online", AsyncMock(return_value=False)):
        await emit(None, "chat_completed", user_id=user.id, resident_id=boss.id, turns=2)
        await db_session.refresh(c)
        assert c.status == "accepted"  # not enough turns
        await emit(None, "chat_completed", user_id=user.id, resident_id=boss.id, turns=4)
        await db_session.refresh(c)
        assert c.status == "completed"


@pytest.mark.anyio
async def test_visit_completion(db_session):
    from app.services.commission_service import accept, check_visit_commissions
    boss = await _resident(db_session, "boss")
    user = await _user(db_session, "v@c.com")
    c = await _commission(db_session, boss.id, "visit_location", {"location_id": "academy"})
    await accept(db_session, c.id, user.id)

    with patch("app.services.notification_service.manager.is_online", AsyncMock(return_value=False)):
        await check_visit_commissions(db_session, user.id, "academy")
    await db_session.refresh(c)
    assert c.status == "completed"


@pytest.mark.anyio
async def test_expire_commissions(db_session):
    from app.services.commission_service import expire_commissions
    boss = await _resident(db_session, "boss")
    await _commission(db_session, boss.id, "chat_topic", {"target_slug": "boss"}, expires_in_h=-2)
    n = await expire_commissions(db_session)
    assert n == 1
    c = (await db_session.execute(select(Commission))).scalar_one()
    assert c.status == "expired"
