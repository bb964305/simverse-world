"""Cross-process WebSocket connection manager (P0-3b).

Before P0-3b all connection state (online players, positions, resident chat
locks and queues) lived in this process's memory, which capped the system at a
single worker: a second API worker or the standalone agent-worker could not see
those locks and its ``broadcast`` calls were dead letters.

Now the only process-local state is ``self.local`` — the actual WebSocket
objects for clients connected to *this* worker (WebSockets cannot be shared
across processes). Everything else is in Redis:

- ``sv:positions``        hash    user_id -> {x, y, direction, name}  (also the online set)
- ``sv:chatting:{rid}``   string  -> user_id             (player<->NPC chat lock, TTL)
- ``sv:socializing:{rid}``string  -> partner_resident_id (NPC<->NPC social lock, TTL)
- ``sv:queue:{rid}``      list    [user_id, ...]         (waiting-to-chat queue)

Realtime delivery goes over a Redis pub/sub channel (``sv:ws``): ``broadcast``
and cross-worker ``send`` publish an envelope; every API worker runs one
``run_subscriber`` task that relays envelopes to its own local sockets. A
``send`` to a client on the current worker short-circuits to a direct write and
never touches Redis, keeping the common "reply to the caller" path fast.

The lock/queue helpers avoid Lua so they work on both redis-py and fakeredis:
re-entrant locking uses SET NX + a follow-up GET, which is safe because a held
key only changes on explicit unlock or TTL expiry.

Lock TTL（burn-in 工程观察②）：锁从 hash field 改为独立 key 挂 TTL——进程非优雅
退出（OOM/kill -9，finally 不跑）留下的孤锁到期自愈；优雅路径仍由 disconnect/
chat_end 显式释放。玩家锁重入即续期（每条 chat 消息都会重入一次 = 心跳）。
"""
import asyncio
import json
import logging

from fastapi import WebSocket

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

POSITIONS_KEY = "sv:positions"
CHATTING_PREFIX = "sv:chatting:"      # per-resident string key, value=user_id
SOCIALIZING_PREFIX = "sv:socializing:"  # per-resident string key, value=partner_id
QUEUE_PREFIX = "sv:queue:"
WS_CHANNEL = "sv:ws"

# 孤锁自愈上限。玩家锁每条消息重入续期，30 分钟只兜底"进程死了"的情况；
# NPC 互聊单次就几秒-几分钟，10 分钟足够。
CHAT_LOCK_TTL = 1800
SOCIAL_LOCK_TTL = 600


class ConnectionManager:
    def __init__(self):
        # user_id -> ws for clients connected to THIS worker only. WebSocket
        # objects are not serializable, so this stays process-local; all shared
        # state lives in Redis.
        self.local: dict[str, WebSocket] = {}

    # ------------------------------------------------------------------ #
    # Local connection registry                                          #
    # ------------------------------------------------------------------ #
    def register(self, user_id: str, ws: WebSocket) -> None:
        """Register an already-accepted connection (caller owns the accept/auth handshake)."""
        self.local[user_id] = ws

    async def disconnect(self, user_id: str) -> None:
        """Drop a client: forget its socket, presence, held chat lock, and queue spots."""
        self.local.pop(user_id, None)
        r = get_redis()
        await r.hdel(POSITIONS_KEY, user_id)
        async for key in r.scan_iter(match=CHATTING_PREFIX + "*"):
            if await r.get(key) == user_id:
                await r.delete(key)
        await self.cancel_all_queues(user_id)

    # ------------------------------------------------------------------ #
    # Delivery (pub/sub fan-out)                                         #
    # ------------------------------------------------------------------ #
    async def _publish(self, envelope: dict) -> None:
        try:
            await get_redis().publish(WS_CHANNEL, json.dumps(envelope))
        except Exception:
            logger.warning("WS publish failed for %s", envelope.get("op"), exc_info=True)

    async def _send_local(self, user_id: str, data: dict) -> bool:
        """Write to a locally-connected socket. Returns False if not local/failed."""
        ws = self.local.get(user_id)
        if ws is None:
            return False
        try:
            await ws.send_json(data)
            return True
        except Exception:
            await self.disconnect(user_id)
            return False

    async def send(self, user_id: str, data: dict) -> None:
        """Send to one user, wherever they are connected.

        Fast path: if the user is on this worker, write directly. Otherwise
        publish a targeted envelope for the owning worker to deliver.
        """
        if user_id in self.local:
            await self._send_local(user_id, data)
            return
        await self._publish({"op": "direct", "user_id": user_id, "data": data})

    async def broadcast(self, data: dict, exclude: str | None = None) -> None:
        """Fan out to every connected client across all workers via pub/sub.

        The publishing worker's own subscriber delivers to its local clients too,
        so we do not also send locally here (that would double-deliver).
        """
        await self._publish({"op": "broadcast", "data": data, "exclude": exclude})

    async def _deliver(self, envelope: dict) -> None:
        """Relay one received pub/sub envelope to this worker's local sockets."""
        op = envelope.get("op")
        data = envelope.get("data")
        if op == "direct":
            await self._send_local(envelope.get("user_id", ""), data)
        elif op == "broadcast":
            exclude = envelope.get("exclude")
            for uid in list(self.local.keys()):
                if uid == exclude:
                    continue
                await self._send_local(uid, data)

    async def run_subscriber(self) -> None:
        """Long-lived task (one per API worker): relay pub/sub → local sockets.

        Resilient to transient Redis errors so a blip never kills the loop.
        """
        while True:
            pubsub = None
            try:
                pubsub = get_redis().pubsub()
                await pubsub.subscribe(WS_CHANNEL)
                logger.info("WS pub/sub subscriber listening on %s", WS_CHANNEL)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    try:
                        envelope = json.loads(message["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    try:
                        await self._deliver(envelope)
                    except Exception:
                        logger.warning("WS deliver failed", exc_info=True)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("WS subscriber error; retrying", exc_info=True)
                await asyncio.sleep(1.0)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass

    # ------------------------------------------------------------------ #
    # Presence / positions                                               #
    # ------------------------------------------------------------------ #
    async def update_position(
        self, user_id: str, x: float, y: float, direction: str, name: str
    ) -> None:
        await get_redis().hset(
            POSITIONS_KEY,
            user_id,
            json.dumps({"x": x, "y": y, "direction": direction, "name": name}),
        )

    async def get_position(self, user_id: str) -> dict | None:
        raw = await get_redis().hget(POSITIONS_KEY, user_id)
        return json.loads(raw) if raw else None

    async def is_online(self, user_id: str) -> bool:
        return bool(await get_redis().hexists(POSITIONS_KEY, user_id))

    async def get_online_players(self, exclude: str | None = None) -> list[dict]:
        data = await get_redis().hgetall(POSITIONS_KEY)
        players: list[dict] = []
        for uid, raw in data.items():
            if uid == exclude:
                continue
            try:
                players.append({"player_id": uid, **json.loads(raw)})
            except (json.JSONDecodeError, TypeError):
                continue
        return players

    # ------------------------------------------------------------------ #
    # Resident chat lock (player <-> NPC)                                #
    # ------------------------------------------------------------------ #
    async def lock_resident(self, resident_id: str, user_id: str) -> bool:
        """Lock resident for chatting. Returns False if held by another user.

        Re-entrant: the same user re-locking their own resident returns True
        AND refreshes the TTL (every chat message re-locks, so an active chat
        never expires; an orphaned lock self-heals after CHAT_LOCK_TTL).
        SET NX is atomic; the follow-up GET is safe because a held key only
        changes on explicit unlock or TTL expiry.
        """
        r = get_redis()
        key = CHATTING_PREFIX + resident_id
        if await r.set(key, user_id, nx=True, ex=CHAT_LOCK_TTL):
            return True
        if (await r.get(key)) == user_id:
            await r.expire(key, CHAT_LOCK_TTL)  # 重入=心跳续期
            return True
        return False

    async def unlock_resident(self, resident_id: str) -> None:
        await get_redis().delete(CHATTING_PREFIX + resident_id)

    # ------------------------------------------------------------------ #
    # Chat queue                                                         #
    # ------------------------------------------------------------------ #
    async def enqueue(self, resident_id: str, user_id: str) -> int:
        """Add user to the chat queue for a resident. Returns 1-based position."""
        r = get_redis()
        key = QUEUE_PREFIX + resident_id
        pos = await r.lpos(key, user_id)
        if pos is None:
            await r.rpush(key, user_id)
            pos = (await r.llen(key)) - 1
        return pos + 1

    async def dequeue(self, resident_id: str) -> str | None:
        """Pop the next still-online waiting user. Returns user_id or None."""
        r = get_redis()
        key = QUEUE_PREFIX + resident_id
        while True:
            user_id = await r.lpop(key)
            if user_id is None:
                return None
            if await self.is_online(user_id):
                return user_id

    async def remove_from_queue(self, resident_id: str, user_id: str) -> None:
        await get_redis().lrem(QUEUE_PREFIX + resident_id, 0, user_id)

    async def cancel_all_queues(self, user_id: str) -> None:
        """Remove a user from every resident queue they are waiting in."""
        r = get_redis()
        async for key in r.scan_iter(match=QUEUE_PREFIX + "*"):
            await r.lrem(key, 0, user_id)

    # ------------------------------------------------------------------ #
    # Social lock (NPC <-> NPC)                                          #
    # ------------------------------------------------------------------ #
    async def lock_socializing(self, res_a_id: str, res_b_id: str) -> bool:
        """Mark two residents as socializing with each other.

        Returns False if either is already locked. Both keys carry a TTL so a
        non-graceful worker death cannot leave the pair locked forever.
        """
        r = get_redis()
        key_a = SOCIALIZING_PREFIX + res_a_id
        key_b = SOCIALIZING_PREFIX + res_b_id
        if not await r.set(key_a, res_b_id, nx=True, ex=SOCIAL_LOCK_TTL):
            return False
        if not await r.set(key_b, res_a_id, nx=True, ex=SOCIAL_LOCK_TTL):
            await r.delete(key_a)  # roll back the half-taken pair
            return False
        return True

    async def unlock_socializing(self, res_a_id: str, res_b_id: str) -> None:
        await get_redis().delete(
            SOCIALIZING_PREFIX + res_a_id, SOCIALIZING_PREFIX + res_b_id
        )

    async def is_socializing(self, resident_id: str) -> bool:
        return bool(await get_redis().exists(SOCIALIZING_PREFIX + resident_id))


manager = ConnectionManager()
