"""E7 time capsules: seal a letter with a resident, deliver on the due date."""

import random
import logging
from datetime import datetime, date as date_type, timedelta, UTC

from sqlalchemy import select

from app.models.resident import Resident
from app.models.time_capsule import TimeCapsule

logger = logging.getLogger(__name__)

MAX_CONTENT = 500
MIN_DAYS = 3
MAX_DAYS = 365
CAPSULE_FEE = 10  # first capsule free, then a fee (simplified from the capsule_ticket item)

NOTES = [
    "这封信我守了好些日子，一个字都没偷看……好吧，我瞄了一眼开头。",
    "你把它交给我的那天，我就一直放在心上。今天，物归原主。",
    "时间过得真快，说好要保管到今天，我做到了。",
    "有时候我会想，写下这些话的你，和现在的你，是同一个人吗？",
]


class CapsuleError(Exception):
    """Raised for invalid capsule requests (router maps to 400)."""


def serialize(c: TimeCapsule, *, include_content: bool) -> dict:
    return {
        "id": c.id,
        "carrier_resident_slug": c.carrier_resident_slug,
        "deliver_on": str(c.deliver_on),
        "status": c.status,
        "content": c.content if (include_content or c.status == "delivered") else None,
        "resident_note": c.resident_note,
        "delivered_at": c.delivered_at.isoformat() if c.delivered_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


async def create_capsule(db, user_id, carrier_slug, deliver_on: date_type, content: str) -> TimeCapsule:
    content = (content or "").strip()
    if not content:
        raise CapsuleError("content is required")
    if len(content) > MAX_CONTENT:
        raise CapsuleError(f"content too long (max {MAX_CONTENT})")
    today = datetime.now(UTC).date()
    if not (today + timedelta(days=MIN_DAYS) <= deliver_on <= today + timedelta(days=MAX_DAYS)):
        raise CapsuleError("deliver_on must be 3 days to 1 year from now")
    carrier = (await db.execute(select(Resident).where(Resident.slug == carrier_slug))).scalar_one_or_none()
    if carrier is None:
        raise CapsuleError("carrier resident not found")

    from sqlalchemy import func
    made = (await db.execute(
        select(func.count()).select_from(TimeCapsule).where(TimeCapsule.user_id == user_id)
    )).scalar() or 0
    if made > 0:
        from app.services.coin_service import charge
        if not await charge(db, user_id, CAPSULE_FEE, "capsule_fee"):
            raise CapsuleError("Insufficient Soul Coins")

    capsule = TimeCapsule(user_id=user_id, carrier_resident_slug=carrier_slug,
                          deliver_on=deliver_on, content=content, status="sealed")
    db.add(capsule)
    await db.commit()
    await db.refresh(capsule)

    try:
        from app.memory.service import MemoryService
        await MemoryService(db).add_memory(
            carrier.id, "event", "有人托我保管一封信，说好了到时候才能拆。",
            importance=0.6, source="capsule", related_user_id=user_id,
        )
    except Exception:
        logger.warning("capsule carrier memory failed", exc_info=True)
    return capsule


async def deliver_due_capsules(db, today: date_type | None = None) -> int:
    """Deliver every sealed capsule whose date has come. Idempotent (status flip)."""
    today = today or datetime.now(UTC).date()
    due = (await db.execute(
        select(TimeCapsule).where(TimeCapsule.deliver_on <= today, TimeCapsule.status == "sealed")
    )).scalars().all()
    if not due:
        return 0

    from app.services.notification_service import notify
    for c in due:
        c.resident_note = random.choice(NOTES)
        c.status = "delivered"
        c.delivered_at = datetime.now(UTC)
    await db.commit()

    for c in due:
        try:
            await notify(
                db, c.user_id, "capsule_delivered", "一封时间胶囊到了",
                c.content[:80], {"content": c.content, "note": c.resident_note, "carrier": c.carrier_resident_slug},
            )
        except Exception:
            logger.warning("capsule delivery notify failed", exc_info=True)
    return len(due)
