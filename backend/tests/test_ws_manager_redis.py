"""P0-3b: Redis-backed ConnectionManager (locks, queues, presence) + pub/sub.

Uses the autouse fakeredis server from conftest, so every method actually
round-trips through Redis exactly as it would in production.
"""
import asyncio

import pytest

from app.ws.manager import ConnectionManager


class FakeWS:
    """Captures send_json payloads; optionally raises to simulate a dead socket."""

    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    async def send_json(self, data: dict) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(data)


@pytest.fixture
def mgr():
    return ConnectionManager()


# ── resident chat lock ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_lock_resident_is_exclusive_and_reentrant(mgr):
    assert await mgr.lock_resident("r1", "userA") is True
    # another user cannot take a held lock
    assert await mgr.lock_resident("r1", "userB") is False
    # the holder re-locking is fine (re-entrant)
    assert await mgr.lock_resident("r1", "userA") is True
    # after unlock, anyone can take it
    await mgr.unlock_resident("r1")
    assert await mgr.lock_resident("r1", "userB") is True


# ── chat queue ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_enqueue_returns_position_and_dedupes(mgr):
    assert await mgr.enqueue("r1", "userA") == 1
    assert await mgr.enqueue("r1", "userB") == 2
    # re-enqueue keeps the original position, no duplicate
    assert await mgr.enqueue("r1", "userA") == 1


@pytest.mark.anyio
async def test_dequeue_skips_offline_users(mgr):
    await mgr.enqueue("r1", "offline-user")
    await mgr.enqueue("r1", "online-user")
    # only online-user has presence
    await mgr.update_position("online-user", 1, 2, "down", "Online")
    assert await mgr.dequeue("r1") == "online-user"
    # queue now empty
    assert await mgr.dequeue("r1") is None


@pytest.mark.anyio
async def test_cancel_all_queues_removes_user_everywhere(mgr):
    await mgr.enqueue("r1", "userA")
    await mgr.enqueue("r2", "userA")
    await mgr.enqueue("r2", "userB")
    await mgr.cancel_all_queues("userA")
    # userA gone from both; userB (now front of r2) still there
    await mgr.update_position("userB", 0, 0, "down", "B")
    assert await mgr.dequeue("r1") is None
    assert await mgr.dequeue("r2") == "userB"


# ── presence ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_presence_and_online_players(mgr):
    await mgr.update_position("u1", 10, 20, "up", "One")
    await mgr.update_position("u2", 30, 40, "down", "Two")
    assert await mgr.is_online("u1") is True
    assert await mgr.is_online("nobody") is False
    assert await mgr.get_position("u1") == {"x": 10, "y": 20, "direction": "up", "name": "One"}
    players = await mgr.get_online_players(exclude="u1")
    assert players == [{"player_id": "u2", "x": 30, "y": 40, "direction": "down", "name": "Two"}]


@pytest.mark.anyio
async def test_disconnect_clears_presence_lock_and_queue(mgr):
    mgr.register("u1", FakeWS())
    await mgr.update_position("u1", 1, 2, "down", "One")
    await mgr.lock_resident("r1", "u1")
    await mgr.enqueue("r2", "u1")

    await mgr.disconnect("u1")

    assert "u1" not in mgr.local
    assert await mgr.is_online("u1") is False
    # lock released -> someone else can take it
    assert await mgr.lock_resident("r1", "u2") is True
    # queue spot removed
    assert await mgr.dequeue("r2") is None


# ── lock TTL（burn-in 工程观察②：孤锁自愈）────────────────────────────

@pytest.mark.anyio
async def test_chat_lock_has_ttl_and_reentry_refreshes(mgr):
    """非优雅退出（OOM/kill -9）留下的孤锁靠 TTL 到期自清；重入续期=心跳。"""
    from app.redis_client import get_redis

    assert await mgr.lock_resident("r1", "userA") is True
    r = get_redis()
    ttl = await r.ttl("sv:chatting:r1")
    assert ttl > 0

    await r.expire("sv:chatting:r1", 5)  # 模拟快过期
    assert await mgr.lock_resident("r1", "userA") is True  # 重入
    assert await r.ttl("sv:chatting:r1") > 5  # 重入把 TTL 续满


@pytest.mark.anyio
async def test_socializing_lock_has_ttl(mgr):
    from app.redis_client import get_redis

    assert await mgr.lock_socializing("a", "b") is True
    r = get_redis()
    assert await r.ttl("sv:socializing:a") > 0
    assert await r.ttl("sv:socializing:b") > 0


# ── socializing (NPC<->NPC) lock ──────────────────────────────────────

@pytest.mark.anyio
async def test_socializing_lock(mgr):
    assert await mgr.lock_socializing("a", "b") is True
    assert await mgr.is_socializing("a") is True
    assert await mgr.is_socializing("b") is True
    # neither can be re-locked while paired
    assert await mgr.lock_socializing("b", "c") is False
    await mgr.unlock_socializing("a", "b")
    assert await mgr.is_socializing("a") is False
    assert await mgr.lock_socializing("b", "c") is True


# ── pub/sub delivery ──────────────────────────────────────────────────

async def _wait_for(predicate, timeout=2.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.anyio
async def test_broadcast_delivers_to_local_sockets_respecting_exclude(mgr):
    ws1, ws2 = FakeWS(), FakeWS()
    mgr.register("u1", ws1)
    mgr.register("u2", ws2)

    sub = asyncio.create_task(mgr.run_subscriber())
    try:
        # give the subscriber a moment to actually SUBSCRIBE
        await asyncio.sleep(0.1)
        await mgr.broadcast({"type": "ping"}, exclude="u2")
        assert await _wait_for(lambda: ws1.sent)
        assert ws1.sent == [{"type": "ping"}]
        assert ws2.sent == []  # excluded
    finally:
        sub.cancel()
        await asyncio.gather(sub, return_exceptions=True)


@pytest.mark.anyio
async def test_send_local_is_direct_without_subscriber(mgr):
    """send() to a locally-connected user writes directly (no pub/sub hop)."""
    ws = FakeWS()
    mgr.register("u1", ws)
    await mgr.send("u1", {"type": "hello"})
    assert ws.sent == [{"type": "hello"}]
