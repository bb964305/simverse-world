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
import time

from fastapi import WebSocket

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

POSITIONS_KEY = "sv:positions"
# REST-controlled Agent players have no WebSocket disconnect hook. Keep their
# presence in a separate hash with an expiry index, then reap it explicitly so
# a dead local Agent cannot remain a permanent online ghost.
AGENT_POSITIONS_KEY = "sv:agent-positions"
AGENT_PRESENCE_EXPIRY_KEY = "sv:agent-presence-expiry"
AGENT_PRESENCE_VERSION_KEY = "sv:agent-presence-version"
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
        # A WebSocket disconnect owns only the ordinary WS presence hash.
        # Headless-Agent leases are refreshed/reaped independently; deleting
        # them here could race a concurrent REST heartbeat and erase a fresh
        # lease for the same principal.
        await r.hdel(POSITIONS_KEY, user_id)
        async for key in r.scan_iter(match=CHATTING_PREFIX + "*"):
            resident_id = key.removeprefix(CHATTING_PREFIX)
            await self.unlock_resident(resident_id, expected_owner=user_id)
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

    async def update_agent_position(
        self,
        user_id: str,
        x: float,
        y: float,
        direction: str,
        name: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        """Refresh expiring headless-Agent presence.

        Returns True when the lease was absent/expired, allowing the caller to
        emit one ``player_joined`` frame. Durable position remains in the DB;
        this is only the realtime online projection.
        """
        r = get_redis()
        now = time.time()
        ttl = max(1, int(ttl_seconds))
        # WATCH closes the reaper-vs-heartbeat race: a concurrent renewal
        # changes the version hash and makes either transaction retry instead
        # of deleting a fresh payload.
        while True:
            async with r.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(
                        AGENT_PRESENCE_EXPIRY_KEY, AGENT_PRESENCE_VERSION_KEY
                    )
                    previous_expiry = await pipe.zscore(
                        AGENT_PRESENCE_EXPIRY_KEY, user_id
                    )
                    became_online = (
                        previous_expiry is None or float(previous_expiry) <= now
                    )
                    current_version = await pipe.hget(
                        AGENT_PRESENCE_VERSION_KEY, user_id
                    )
                    version = int(current_version or 0) + 1
                    payload = {
                        "x": x,
                        "y": y,
                        "direction": direction,
                        "name": name,
                        "agent_controlled": True,
                        "presence_ttl_seconds": ttl,
                    }
                    pipe.multi()
                    pipe.hset(
                        AGENT_POSITIONS_KEY, user_id, json.dumps(payload)
                    )
                    pipe.hset(AGENT_PRESENCE_VERSION_KEY, user_id, version)
                    pipe.zadd(AGENT_PRESENCE_EXPIRY_KEY, {user_id: now + ttl})
                    await pipe.execute()
                    break
                except Exception as exc:
                    from redis.exceptions import WatchError

                    if isinstance(exc, WatchError):
                        continue
                    raise
        return became_online

    async def get_position(self, user_id: str) -> dict | None:
        r = get_redis()
        raw = await r.hget(POSITIONS_KEY, user_id)
        return json.loads(raw) if raw else None

    async def get_visible_position(self, user_id: str) -> dict | None:
        """Map projection position for either a WS player or leased Agent."""
        raw = await self.get_position(user_id)
        if raw is not None:
            return raw
        r = get_redis()
        expiry = await r.zscore(AGENT_PRESENCE_EXPIRY_KEY, user_id)
        if expiry is not None and float(expiry) > time.time():
            agent_raw = await r.hget(AGENT_POSITIONS_KEY, user_id)
            return json.loads(agent_raw) if agent_raw else None
        return None

    async def is_online(self, user_id: str) -> bool:
        r = get_redis()
        # Only a live WebSocket can receive legacy player-chat/notification
        # frames. Headless Agents have their own durable inbox; their map lease
        # must not make those WS-only services try a pub/sub delivery that no
        # socket can consume.
        if await r.hexists(POSITIONS_KEY, user_id):
            return True
        return False

    async def online_user_ids(self) -> set[str]:
        """Bulk projection of :meth:`is_online` — one HKEYS round-trip.

        Reads POSITIONS_KEY only, so headless Agent leases are deliberately
        excluded, exactly like the per-user check above.
        """
        r = get_redis()
        return set(await r.hkeys(POSITIONS_KEY))

    async def get_online_players(self, exclude: str | None = None) -> list[dict]:
        r = get_redis()
        data = await r.hgetall(POSITIONS_KEY)
        agent_data = await r.hgetall(AGENT_POSITIONS_KEY)
        now = time.time()
        players: list[dict] = []
        combined: list[tuple[str, str]] = list(data.items())
        for uid, raw in agent_data.items():
            expiry = await r.zscore(AGENT_PRESENCE_EXPIRY_KEY, uid)
            if expiry is not None and float(expiry) > now and uid not in data:
                combined.append((uid, raw))
        for uid, raw in combined:
            if uid == exclude:
                continue
            try:
                players.append({"player_id": uid, **json.loads(raw)})
            except (json.JSONDecodeError, TypeError):
                continue
        return players

    async def expire_agent_presences(self, *, now: float | None = None) -> list[str]:
        """Atomically remove expired leases and enqueue ordered leave events."""
        r = get_redis()
        cutoff = time.time() if now is None else now
        expired = await r.zrangebyscore(AGENT_PRESENCE_EXPIRY_KEY, "-inf", cutoff)
        removed: list[str] = []
        for user_id in expired:
            while True:
                async with r.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(
                            AGENT_PRESENCE_EXPIRY_KEY,
                            AGENT_PRESENCE_VERSION_KEY,
                        )
                        expiry = await pipe.zscore(
                            AGENT_PRESENCE_EXPIRY_KEY, user_id
                        )
                        if expiry is None or float(expiry) > cutoff:
                            await pipe.reset()
                            break
                        # Read the version while watched. Its value need not be
                        # returned; watching the key prevents deletion if a
                        # heartbeat increments it before EXEC.
                        await pipe.hget(AGENT_PRESENCE_VERSION_KEY, user_id)
                        pipe.multi()
                        pipe.zrem(AGENT_PRESENCE_EXPIRY_KEY, user_id)
                        pipe.hdel(AGENT_POSITIONS_KEY, user_id)
                        pipe.hdel(AGENT_PRESENCE_VERSION_KEY, user_id)
                        # Publish inside the same Redis transaction. A racing
                        # heartbeat is therefore totally ordered: either leave
                        # precedes its later joined event, or the WATCH retry
                        # observes the renewed expiry and emits no stale leave.
                        pipe.publish(
                            WS_CHANNEL,
                            json.dumps(
                                {
                                    "op": "broadcast",
                                    "data": {
                                        "type": "player_left",
                                        "player_id": user_id,
                                    },
                                    "exclude": None,
                                }
                            ),
                        )
                        result = await pipe.execute()
                        if result and result[0]:
                            removed.append(user_id)
                        break
                    except Exception as exc:
                        from redis.exceptions import WatchError

                        if isinstance(exc, WatchError):
                            continue
                        raise
        return removed

    async def run_agent_presence_reaper(self) -> None:
        """Remove expired headless-Agent leases and notify live browsers."""
        while True:
            try:
                await self.expire_agent_presences()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Agent presence reaper failed", exc_info=True)
            await asyncio.sleep(10)

    # ------------------------------------------------------------------ #
    # Resident chat lock (player <-> NPC)                                #
    # ------------------------------------------------------------------ #
    async def lock_resident(
        self,
        resident_id: str,
        user_id: str,
        *,
        ttl_seconds: int = CHAT_LOCK_TTL,
    ) -> bool:
        """Lock resident for chatting. Returns False if held by another user.

        Re-entrant: the same user re-locking their own resident returns True
        AND refreshes the TTL (every chat message re-locks, so an active chat
        never expires; an orphaned lock self-heals after CHAT_LOCK_TTL).
        SET NX is atomic; the follow-up GET is safe because a held key only
        changes on explicit unlock or TTL expiry.
        """
        r = get_redis()
        key = CHATTING_PREFIX + resident_id
        ttl = max(1, int(ttl_seconds))
        if await r.set(key, user_id, nx=True, ex=ttl):
            return True
        if (await r.get(key)) == user_id:
            await r.expire(key, ttl)  # 重入=心跳续期
            return True
        return False

    async def unlock_resident(
        self, resident_id: str, *, expected_owner: str | None = None
    ) -> bool:
        """Release a resident lock, optionally only when still owned by caller.

        The owner-checked form prevents a stale disconnect/finalizer from
        deleting a lease that expired and was subsequently acquired by another
        human or external Agent.
        """
        r = get_redis()
        key = CHATTING_PREFIX + resident_id
        if expected_owner is None:
            return bool(await r.delete(key))
        while True:
            async with r.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    if await pipe.get(key) != expected_owner:
                        await pipe.reset()
                        return False
                    pipe.multi()
                    pipe.delete(key)
                    result = await pipe.execute()
                    return bool(result and result[0])
                except Exception as exc:
                    from redis.exceptions import WatchError

                    if isinstance(exc, WatchError):
                        continue
                    raise

    async def resident_lock_owner(self, resident_id: str) -> str | None:
        """Return the current chat-lock owner for safe queue handoff."""
        return await get_redis().get(CHATTING_PREFIX + resident_id)

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
