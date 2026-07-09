"""E12 season scorer: accumulate season points off the S2 bus.

chat_completed scores only the first 5 chats/day (tracked in Redis); the overall
100/day cap in season_service is the hard ceiling.
"""

import logging
from datetime import datetime, UTC

from app.events.bus import on
from app.redis_client import get_redis
from app.services.season_service import add_points

logger = logging.getLogger(__name__)

CHAT_DAILY_SCORED = 5


@on("chat_completed")
async def _score_chat(db, user_id: str = "", **kw) -> None:
    if not user_id:
        return
    r = get_redis()
    key = f"season_chat:{user_id}:{datetime.now(UTC).date().isoformat()}"
    n = await r.incr(key)
    if n == 1:
        await r.expire(key, 2 * 86400)
    if n <= CHAT_DAILY_SCORED:
        await add_points(user_id, 5, "chat")


@on("commission_completed")
async def _score_commission(db, user_id: str = "", **kw) -> None:
    if user_id:
        await add_points(user_id, 15, "commission")


@on("location_first_visit")
async def _score_location(db, user_id: str = "", **kw) -> None:
    if user_id:
        await add_points(user_id, 10, "explore")
