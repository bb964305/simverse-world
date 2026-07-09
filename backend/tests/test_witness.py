"""E4 witness memories: nearby online players, 4h dedup, cap 20, distance."""

from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.memory import Memory


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
async def test_nearby_player_writes_witness(db_session, witness_env):
    ws = witness_env
    with patch.object(ws.manager, "get_online_players",
                      AsyncMock(return_value=_players(("u1", 5, 5, "玩家A")))):
        n = await ws.record_witnesses("res1", 5, 5, None)
    assert n == 1

    mems = (await db_session.execute(
        select(Memory).where(Memory.source == "witness", Memory.resident_id == "res1")
    )).scalars().all()
    assert len(mems) == 1
    assert mems[0].importance == 0.25 and mems[0].related_user_id == "u1"
    assert "看到玩家A" in mems[0].content


@pytest.mark.anyio
async def test_dedup_within_4h(db_session, witness_env):
    ws = witness_env
    players = _players(("u1", 5, 5, "玩家A"))
    with patch.object(ws.manager, "get_online_players", AsyncMock(return_value=players)):
        assert await ws.record_witnesses("res1", 5, 5, None) == 1
        assert await ws.record_witnesses("res1", 5, 5, None) == 0  # deduped

    count = (await db_session.execute(
        select(func.count()).select_from(Memory).where(Memory.source == "witness")
    )).scalar()
    assert count == 1


@pytest.mark.anyio
async def test_offline_no_witness(witness_env):
    ws = witness_env
    with patch.object(ws.manager, "get_online_players", AsyncMock(return_value=[])):
        assert await ws.record_witnesses("res1", 5, 5, None) == 0


@pytest.mark.anyio
async def test_far_player_ignored(witness_env):
    ws = witness_env
    with patch.object(ws.manager, "get_online_players",
                      AsyncMock(return_value=_players(("u1", 100, 100, "远方")))):
        assert await ws.record_witnesses("res1", 5, 5, None) == 0


@pytest.mark.anyio
async def test_cap_prunes_to_20(db_session, witness_env):
    ws = witness_env
    # Pre-seed 20 witness memories.
    base = datetime.now(UTC) - timedelta(hours=5)
    for i in range(20):
        db_session.add(Memory(
            resident_id="res1", type="event", content=f"旧{i}", importance=0.25,
            source="witness", related_user_id="old", created_at=base + timedelta(minutes=i),
        ))
    await db_session.commit()

    with patch.object(ws.manager, "get_online_players",
                      AsyncMock(return_value=_players(("u2", 5, 5, "新玩家")))):
        await ws.record_witnesses("res1", 5, 5, None)

    count = (await db_session.execute(
        select(func.count()).select_from(Memory).where(
            Memory.resident_id == "res1", Memory.source == "witness",
        )
    )).scalar()
    assert count == 20  # 20 + 1 new, oldest pruned
