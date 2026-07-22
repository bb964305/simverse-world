"""Realism P0-5c: chat/encounter/witness cooldowns live in Redis (cross-worker,
survives restart) — asserted via the shared fakeredis store, not a process dict."""
import pytest

from app.redis_client import get_redis
from app.models.resident import Resident


@pytest.mark.anyio
async def test_chat_cooldown_in_redis():
    from app.agent import chat
    a = Resident(id="ra", slug="a", name="A", creator_id="s")
    b = Resident(id="rb", slug="b", name="B", creator_id="s")
    assert await chat._is_on_cooldown(a, b) is False
    await chat._set_cooldown(a, b)
    # Present in Redis under a symmetric pair key (order-independent).
    assert await get_redis().exists(chat._pair_key(a, b))
    assert await get_redis().exists(chat._pair_key(b, a))
    assert await chat._is_on_cooldown(a, b) is True
    assert await chat._is_on_cooldown(b, a) is True


@pytest.mark.anyio
async def test_encounter_daily_counter_in_redis():
    from app.services import encounter_service as es
    r = get_redis()
    key = es._daily_key("u1", "2026-07-22")
    assert await r.get(key) is None
    await r.incr(key)
    await r.incr(key)
    assert int(await r.get(key)) == 2


@pytest.mark.anyio
async def test_witness_dedup_set_nx_is_atomic():
    from app.services import witness_service as ws
    r = get_redis()
    key = ws._witness_key("res-1", "user-1")
    assert await r.set(key, "1", ex=ws.WITNESS_DEDUP_SECONDS, nx=True) is True
    # Second SET NX within the window returns falsey (already witnessed).
    assert not await r.set(key, "1", ex=ws.WITNESS_DEDUP_SECONDS, nx=True)
