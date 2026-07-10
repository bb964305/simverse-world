"""Ad-hoc P0-3b smoke against a REAL redis-server (not fakeredis).

Run: REDIS_URL=redis://localhost:6399/0 python scripts_redis_smoke.py
Exercises manager locks/queues/presence/pubsub, the rate limiter and the
agent daily counter directly against a live server. Not part of the pytest
suite (kept out of tests/). Exit 0 = all assertions passed.
"""
import asyncio
import os

os.environ.setdefault("DEBUG", "true")

import redis.asyncio as aioredis

from app.redis_client import set_redis, get_redis
from app.ws.manager import ConnectionManager
from app.ws.rate_limiter import SlidingWindowLimiter
from app.config import settings


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


async def main():
    url = os.environ.get("REDIS_URL", "redis://localhost:6399/0")
    client = aioredis.from_url(url, decode_responses=True)
    await client.flushdb()
    set_redis(client)
    mgr = ConnectionManager()

    # ── locks: exclusive + re-entrant ──
    assert await mgr.lock_resident("r1", "A") is True
    assert await mgr.lock_resident("r1", "B") is False
    assert await mgr.lock_resident("r1", "A") is True
    await mgr.unlock_resident("r1")
    assert await mgr.lock_resident("r1", "B") is True

    # ── queue: position, dedupe, dequeue skips offline ──
    assert await mgr.enqueue("r2", "off") == 1
    assert await mgr.enqueue("r2", "on") == 2
    assert await mgr.enqueue("r2", "off") == 1  # dedupe
    await mgr.update_position("on", 1, 2, "down", "On")
    assert await mgr.dequeue("r2") == "on"  # off skipped (not online)
    assert await mgr.dequeue("r2") is None

    # ── presence / online players ──
    await mgr.update_position("u1", 5, 6, "up", "U1")
    assert await mgr.is_online("u1") is True
    assert await mgr.get_position("u1") == {"x": 5, "y": 6, "direction": "up", "name": "U1"}
    players = await mgr.get_online_players(exclude="on")
    assert {p["player_id"] for p in players} == {"u1"}

    # ── cancel_all_queues (scan) ──
    await mgr.enqueue("qa", "victim")
    await mgr.enqueue("qb", "victim")
    await mgr.cancel_all_queues("victim")
    assert await mgr.dequeue("qa") is None and await mgr.dequeue("qb") is None

    # ── disconnect clears lock + presence + queue ──
    mgr.register("d1", FakeWS())
    await mgr.update_position("d1", 0, 0, "down", "D1")
    await mgr.lock_resident("rd", "d1")
    await mgr.enqueue("rq", "d1")
    await mgr.disconnect("d1")
    assert await mgr.is_online("d1") is False
    assert await mgr.lock_resident("rd", "other") is True
    assert await mgr.dequeue("rq") is None

    # ── socializing ──
    assert await mgr.lock_socializing("a", "b") is True
    assert await mgr.lock_socializing("b", "c") is False
    assert await mgr.is_socializing("a") is True
    await mgr.unlock_socializing("a", "b")
    assert await mgr.is_socializing("a") is False

    # ── pub/sub broadcast delivery with exclude ──
    ws1, ws2 = FakeWS(), FakeWS()
    mgr.register("p1", ws1)
    mgr.register("p2", ws2)
    sub = asyncio.create_task(mgr.run_subscriber())
    await asyncio.sleep(0.2)
    await mgr.broadcast({"type": "ping"}, exclude="p2")
    for _ in range(50):
        if ws1.sent:
            break
        await asyncio.sleep(0.02)
    assert ws1.sent == [{"type": "ping"}], ws1.sent
    assert ws2.sent == []
    # direct send to a non-local user routes over pub/sub to its owning worker;
    # here p1 is local so send() is a direct write
    await mgr.send("p1", {"type": "direct"})
    assert {"type": "direct"} in ws1.sent
    sub.cancel()
    await asyncio.gather(sub, return_exceptions=True)

    # ── rate limiter sliding window (limit 3) ──
    rl = SlidingWindowLimiter(max_per_minute=3, namespace="smoke")
    assert [await rl.check("k") for _ in range(4)] == [True, True, True, False]

    # ── agent daily counter (real INCR + TTL) ──
    from app.agent.tick import _incr_daily_count, _over_daily_limit, _daily_key
    rid = "res-smoke"
    for _ in range(settings.agent_max_daily_actions):
        await _incr_daily_count(rid)
    assert await _over_daily_limit(rid) is True
    ttl = await get_redis().ttl(_daily_key(rid))
    assert ttl > 0, f"daily key should have a TTL, got {ttl}"

    await client.aclose()
    print("REAL-REDIS SMOKE: ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
