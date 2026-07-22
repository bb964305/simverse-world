"""P2 Task 2 — relation write-path wiring.

Four triggers, each reusing an existing signal (zero new LLM), each gated by
REALISM_RELATIONS_ENABLED (off → zero writes, pre-P2 behavior unchanged):
  a) resident-resident chat wrap-up  → familiarity +0.05, affinity ±0.03 by mood
  b) player↔resident gift            → affinity += relationship_boost
  c) investment                      → affinity +0.1
  d) witness                         → familiarity +0.01
The player-chat familiarity/affinity split (end_chat vs rating) is exercised via
the underlying relation calls the handlers make.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.config import settings
from app.services import relation_service as rel
from app.models.resident_relation import ResidentRelation
from app.models.user import User
from app.models.resident import Resident


async def _count(db) -> int:
    return (await db.execute(select(func.count()).select_from(ResidentRelation))).scalar_one()


# ----------------------------- (a) resident chat --------------------------------

@pytest.mark.anyio
async def test_chat_relations_positive(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    from app.agent.chat import _apply_chat_relations
    a, b = SimpleNamespace(id="ra"), SimpleNamespace(id="rb")
    await _apply_chat_relations(db_session, a, b, "positive")
    r = await rel.get_pair(db_session, "ra", "rb")
    assert r.familiarity == pytest.approx(0.05)
    assert r.affinity == pytest.approx(0.03)


@pytest.mark.anyio
async def test_chat_relations_negative_and_neutral(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    from app.agent.chat import _apply_chat_relations
    await _apply_chat_relations(db_session, SimpleNamespace(id="na"), SimpleNamespace(id="nb"), "negative")
    r = await rel.get_pair(db_session, "na", "nb")
    assert r.familiarity == pytest.approx(0.05) and r.affinity == pytest.approx(-0.03)

    await _apply_chat_relations(db_session, SimpleNamespace(id="xa"), SimpleNamespace(id="xb"), "neutral")
    r2 = await rel.get_pair(db_session, "xa", "xb")
    assert r2.familiarity == pytest.approx(0.05) and r2.affinity == pytest.approx(0.0)


@pytest.mark.anyio
async def test_chat_relations_gated_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", False)
    from app.agent.chat import _apply_chat_relations
    await _apply_chat_relations(db_session, SimpleNamespace(id="ra"), SimpleNamespace(id="rb"), "positive")
    assert await _count(db_session) == 0


# ----------------------------- (b) gift ----------------------------------------

async def _user(db, email, bal=500):
    u = User(name="u", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


@pytest.mark.anyio
async def test_gift_bumps_affinity(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    from app.services.shop_service import purchase, seed_items
    await seed_items(db_session)
    creator = await _user(db_session, "c@g.com", 0)
    buyer = await _user(db_session, "b@g.com", 200)
    r = Resident(slug="klaus", name="K", creator_id=creator.id, district="cafe", status="idle", tile_x=1, tile_y=1)
    db_session.add(r)
    await db_session.commit()

    await purchase(db_session, buyer.id, "gift_flower", 1, {"resident_slug": "klaus"})
    rec = await rel.get_pair(db_session, r.id, buyer.id)
    assert rec is not None
    assert rec.affinity == pytest.approx(0.1)   # gift_flower relationship_boost
    assert rec.familiarity == pytest.approx(0.0)


@pytest.mark.anyio
async def test_gift_gated_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", False)
    from app.services.shop_service import purchase, seed_items
    await seed_items(db_session)
    creator = await _user(db_session, "c2@g.com", 0)
    buyer = await _user(db_session, "b2@g.com", 200)
    r = Resident(slug="klaus2", name="K", creator_id=creator.id, district="cafe", status="idle", tile_x=1, tile_y=1)
    db_session.add(r)
    await db_session.commit()
    await purchase(db_session, buyer.id, "gift_flower", 1, {"resident_slug": "klaus2"})
    assert await _count(db_session) == 0


# ----------------------------- (c) investment ----------------------------------

async def _goal(db, creator_id):
    r = Resident(slug="dora", name="D", creator_id=creator_id, district="cafe", status="idle", tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    from app.services.goal_service import create_goal
    g = await create_goal(db, r.id, "开咖啡馆", "热爱咖啡", kind="life")
    return r, g


@pytest.mark.anyio
async def test_investment_bumps_affinity(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    from app.services.investment_service import invest
    creator = await _user(db_session, "c@inv.com", 0)
    investor = await _user(db_session, "i@inv.com", 1000)
    r, goal = await _goal(db_session, creator.id)
    await invest(db_session, investor.id, goal.id, 100)
    rec = await rel.get_pair(db_session, r.id, investor.id)
    assert rec is not None and rec.affinity == pytest.approx(0.1)


@pytest.mark.anyio
async def test_investment_gated_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", False)
    from app.services.investment_service import invest
    creator = await _user(db_session, "c2@inv.com", 0)
    investor = await _user(db_session, "i2@inv.com", 1000)
    r, goal = await _goal(db_session, creator.id)
    await invest(db_session, investor.id, goal.id, 100)
    assert await _count(db_session) == 0


# ----------------------------- (d) witness -------------------------------------

@pytest.fixture
def witness_env(db_engine):
    from app.services import witness_service as ws
    ws._reset_for_tests()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(ws, "async_session", factory):
        yield ws
    ws._reset_for_tests()


def _players(*specs):
    return [{"player_id": uid, "x": tx * 32, "y": ty * 32, "name": name}
            for uid, tx, ty, name in specs]


@pytest.mark.anyio
async def test_witness_bumps_familiarity(db_session, witness_env, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    ws = witness_env
    with patch.object(ws.manager, "get_online_players",
                      AsyncMock(return_value=_players(("u1", 5, 5, "玩家A")))):
        n = await ws.record_witnesses("res1", 5, 5, None)
    assert n == 1
    rec = await rel.get_pair(db_session, "res1", "u1")
    assert rec is not None and rec.familiarity == pytest.approx(0.01)


@pytest.mark.anyio
async def test_witness_gated_off(db_session, witness_env, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", False)
    ws = witness_env
    with patch.object(ws.manager, "get_online_players",
                      AsyncMock(return_value=_players(("u1", 5, 5, "玩家A")))):
        await ws.record_witnesses("res2", 5, 5, None)
    assert await _count(db_session) == 0


# --------------------- (a') player↔resident WS handlers -------------------------

class _FakeManager:
    async def send(self, *a, **k):
        return None


async def _seed_player_convo(db, uid, rid, cid):
    from app.models.conversation import Conversation
    db.add(User(id=uid, name="U", email=f"{uid}@t.co", soul_coin_balance=10))
    db.add(Resident(id=rid, slug=rid, name="P", creator_id=uid, district="cafe",
                    status="chatting", tile_x=1, tile_y=1, heat=0))
    db.add(Conversation(id=cid, user_id=uid, resident_id=rid))
    await db.commit()


@pytest.mark.anyio
async def test_player_end_chat_bumps_familiarity(db_session, db_engine, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    from app.ws.handlers import chat as chat_handler
    from app.ws.handlers.context import ConnectionContext
    await _seed_player_convo(db_session, "pu1", "pr1", "pc1")
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    ctx = ConnectionContext(user_id="pu1", user_name="U", conversation_id="pc1",
                            resident=SimpleNamespace(id="pr1", slug="pr1"))
    with patch.object(chat_handler, "async_session", factory):
        await chat_handler.handle_end_chat(ctx, {"type": "end_chat"})
    rec = await rel.get_pair(db_session, "pr1", "pu1")
    assert rec is not None and rec.familiarity == pytest.approx(0.05)
    assert rec.affinity == pytest.approx(0.0)   # affinity rides the rating, not end_chat


@pytest.mark.anyio
async def test_player_rating_bumps_affinity(db_session, db_engine, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    from app.ws.handlers import rating as rating_handler
    from app.ws.handlers.context import ConnectionContext
    await _seed_player_convo(db_session, "ru1", "rr1", "rc1")
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    ctx = ConnectionContext(user_id="ru1", user_name="U", conversation_id="rc1",
                            resident=SimpleNamespace(id="rr1", slug="rr1"))
    with patch.object(rating_handler, "async_session", factory), \
         patch.object(rating_handler, "manager", _FakeManager()):
        await rating_handler.handle_rate_chat(ctx, {"conversation_id": "rc1", "rating": 5})
    rec = await rel.get_pair(db_session, "rr1", "ru1")
    assert rec is not None and rec.affinity == pytest.approx(0.03)   # 5★ positive
    assert rec.familiarity == pytest.approx(0.0)   # familiarity rides end_chat


@pytest.mark.anyio
async def test_player_low_rating_negative_affinity(db_session, db_engine, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", True)
    from app.ws.handlers import rating as rating_handler
    from app.ws.handlers.context import ConnectionContext
    await _seed_player_convo(db_session, "lu1", "lr1", "lc1")
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    ctx = ConnectionContext(user_id="lu1", user_name="U", conversation_id="lc1",
                            resident=SimpleNamespace(id="lr1", slug="lr1"))
    with patch.object(rating_handler, "async_session", factory), \
         patch.object(rating_handler, "manager", _FakeManager()):
        await rating_handler.handle_rate_chat(ctx, {"conversation_id": "lc1", "rating": 1})
    rec = await rel.get_pair(db_session, "lr1", "lu1")
    assert rec is not None and rec.affinity == pytest.approx(-0.03)


@pytest.mark.anyio
async def test_player_handlers_gated_off(db_session, db_engine, monkeypatch):
    monkeypatch.setattr(settings, "realism_relations_enabled", False)
    from app.ws.handlers import chat as chat_handler
    from app.ws.handlers import rating as rating_handler
    from app.ws.handlers.context import ConnectionContext
    await _seed_player_convo(db_session, "gu1", "gr1", "gc1")
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    ctx = ConnectionContext(user_id="gu1", user_name="U", conversation_id="gc1",
                            resident=SimpleNamespace(id="gr1", slug="gr1"))
    with patch.object(chat_handler, "async_session", factory):
        await chat_handler.handle_end_chat(ctx, {"type": "end_chat"})
    with patch.object(rating_handler, "async_session", factory), \
         patch.object(rating_handler, "manager", _FakeManager()):
        await rating_handler.handle_rate_chat(ctx, {"conversation_id": "gc1", "rating": 5})
    assert await _count(db_session) == 0
