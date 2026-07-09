"""A3: residents proactively greet returning players on connect.

Runs as a fire-and-forget task off the connection flow (never blocks it). A
player with a strong relationship to an idle resident may receive one greeting
per resident per 24h, optionally with a system gift for a best friend.
"""

import logging
import random
from datetime import datetime, timedelta, UTC

from sqlalchemy import select

from app.database import async_session
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.shop import Item
from app.models.user import User
from app.services.notification_service import notify
from app.ws.manager import manager

logger = logging.getLogger(__name__)

GREET_COOLDOWN_HOURS = 24
GIFT_COOLDOWN_DAYS = 7
CLOSE_FRIEND_IMPORTANCE = 0.85

# Two tone pools; picked by the resident's warmth (heat) as a zero-cost proxy for
# the SBTI extroversion tone (precise SBTI-dimension tone is a later refinement).
WARM_TEMPLATES = [
    "嘿，{player}！好久不见，最近过得怎么样？",
    "{player}，你回来啦！我正想着你呢～",
    "哇，是 {player}！今天要不要一起聊聊天？",
]
RESERVED_TEMPLATES = [
    "……{player}，你来了。有空的话，聊两句？",
    "{player}，好久没见。最近还好吗？",
    "看到你回来，我挺高兴的，{player}。",
]


def _pick_template(resident: Resident) -> str:
    pool = WARM_TEMPLATES if (resident.heat or 0) >= 30 else RESERVED_TEMPLATES
    return random.choice(pool)


async def _recently(db, resident_id: str, user_id: str, source: str, within: timedelta) -> bool:
    cutoff = datetime.now(UTC) - within
    row = (await db.execute(
        select(Memory.id).where(
            Memory.resident_id == resident_id,
            Memory.related_user_id == user_id,
            Memory.source == source,
            Memory.created_at >= cutoff,
        ).limit(1)
    )).scalar_one_or_none()
    return row is not None


async def _maybe_gift(db, resident: Resident, user_id: str) -> dict | None:
    if await _recently(db, resident.id, user_id, "greeting_gift", timedelta(days=GIFT_COOLDOWN_DAYS)):
        return None
    gifts = (await db.execute(
        select(Item).where(Item.kind == "gift", Item.active.is_(True))
    )).scalars().all()
    if not gifts:
        return None
    item = random.choice(gifts)
    # System gift — no charge. Record a cooldown marker memory.
    await MemoryService(db).add_memory(
        resident.id, "event", f"我送了 {item.name} 给一位朋友",
        importance=0.3, source="greeting_gift", related_user_id=user_id,
        metadata_json={"gift": item.code},
    )
    return {"code": item.code, "name": item.name, "icon": item.icon}


async def maybe_greet(user_id: str) -> None:
    """Send at most one resident greeting to a returning player. Best-effort."""
    try:
        async with async_session() as db:
            rels = (await db.execute(
                select(Memory).where(
                    Memory.type == "relationship",
                    Memory.related_user_id == user_id,
                ).order_by(Memory.importance.desc()).limit(3)
            )).scalars().all()
            if not rels:
                return  # new player / no relationship → stay quiet

            player = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
            player_name = player.name if player else "朋友"

            for rel in rels:
                resident = await db.get(Resident, rel.resident_id)
                if resident is None or resident.status != "idle":
                    continue
                if await _recently(db, resident.id, user_id, "greeting", timedelta(hours=GREET_COOLDOWN_HOURS)):
                    continue

                text = _pick_template(resident).format(player=player_name)
                gift = None
                if (rel.importance or 0) >= CLOSE_FRIEND_IMPORTANCE:
                    gift = await _maybe_gift(db, resident, user_id)

                if await manager.is_online(user_id):
                    await manager.send(user_id, {
                        "type": "resident_greeting",
                        "resident_slug": resident.slug,
                        "text": text,
                        "gift": gift,
                    })
                await notify(
                    db, user_id, "resident_greeting", f"{resident.name} 跟你打招呼", text,
                    {"resident_slug": resident.slug, "gift": gift},
                )
                await MemoryService(db).add_memory(
                    resident.id, "event", "我跟一位老朋友打了招呼",
                    importance=0.2, source="greeting", related_user_id=user_id,
                    metadata_json={"greeted_user": user_id},
                )
                return  # one greeting per connection
    except Exception:
        logger.warning("maybe_greet failed for %s", user_id, exc_info=True)
