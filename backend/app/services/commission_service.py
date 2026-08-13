"""B1 commission tasks: lifecycle + completion detection.

Accept uses an optimistic `UPDATE ... WHERE status='open'` so only one of several
concurrent players wins (the rest get 409). Completion is detected off the
chat_completed event (deliver_message / chat_topic) and the location enter path
(visit_location); settlement rewards the player, writes both residents a memory,
notifies, and emits commission_completed (→ D1 errand_runner).
"""

import re
import logging
from datetime import datetime, timedelta, UTC

from sqlalchemy import select, update, func

from app.config import settings
from app.database import async_session
from app.events.bus import on, emit
from app.models.commission import Commission
from app.models.resident import Resident
from app.models.conversation import Message

logger = logging.getLogger(__name__)

DEFAULT_GLOBAL_CAP = 15


class CommissionError(Exception):
    """Raised for accept/abandon conflicts (router maps to 409/400)."""


def _cap() -> int:
    return getattr(settings, "commission_global_cap", DEFAULT_GLOBAL_CAP) or DEFAULT_GLOBAL_CAP


def serialize(c: Commission) -> dict:
    return {
        "id": c.id,
        "issuer_resident_id": c.issuer_resident_id,
        "kind": c.kind,
        "title": c.title,
        "payload": c.payload_json or {},
        "reward_sc": c.reward_sc,
        "status": c.status,
        "acceptor_user_id": c.acceptor_user_id,
        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
    }


async def create_commission(db, issuer_resident_id, kind, title, payload, reward_sc) -> Commission | str | None:
    """Create an open commission. Three-state return:

    - ``None``       — global open-commission cap reached (master semantics);
    - ``"deduped"``  — commission_lifecycle_v2_enabled only: the issuer already
                       has an unexpired open/accepted commission of this kind;
    - ``Commission`` — created and committed.
    """
    if settings.commission_lifecycle_v2_enabled:
        # One active template per issuer/kind. Duty WORK can run daily while a
        # commission now lives commission_ttl_hours; without this guard it posts
        # identical errands before the first one has had a fair chance to settle.
        # Open-but-expired rows must not block a fresh posting.
        duplicate = (await db.execute(
            select(Commission.id).where(
                Commission.issuer_resident_id == issuer_resident_id,
                Commission.kind == kind,
                Commission.status.in_(["open", "accepted"]),
                Commission.expires_at > datetime.now(UTC),
            ).limit(1)
        )).scalar_one_or_none()
        if duplicate is not None:
            return "deduped"
    open_count = (await db.execute(
        select(func.count()).select_from(Commission).where(Commission.status == "open")
    )).scalar() or 0
    if open_count >= _cap():
        return None
    kwargs = {}
    if settings.commission_lifecycle_v2_enabled:
        kwargs["expires_at"] = datetime.now(UTC) + timedelta(
            hours=max(1, int(settings.commission_ttl_hours or 72)))
    c = Commission(
        issuer_resident_id=issuer_resident_id, kind=kind, title=title,
        payload_json=payload, reward_sc=reward_sc, status="open", **kwargs,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def accept(db, commission_id, user_id) -> Commission:
    """Optimistically claim an open, unexpired commission. Raises CommissionError on conflict."""
    now = datetime.now(UTC)
    result = await db.execute(
        update(Commission)
        .where(Commission.id == commission_id, Commission.status == "open", Commission.expires_at > now)
        .values(status="accepted", acceptor_user_id=user_id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount == 0:
        raise CommissionError("Commission is not open (already taken or expired)")
    # populate_existing forces a refresh of the identity-map object (the update
    # ran with synchronize_session=False, so the cached ORM row is stale).
    return (await db.execute(
        select(Commission).where(Commission.id == commission_id).execution_options(populate_existing=True)
    )).scalar_one()


async def abandon(db, commission_id, user_id) -> None:
    c = (await db.execute(select(Commission).where(Commission.id == commission_id))).scalar_one_or_none()
    if c is None or c.acceptor_user_id != user_id or c.status != "accepted":
        raise CommissionError("Cannot abandon this commission")
    c.status = "open"
    c.acceptor_user_id = None
    await db.commit()


async def complete(db, commission: Commission) -> None:
    """Settle a commission: reward, resident memories, notify, emit event."""
    from app.services.coin_service import reward
    from app.services.notification_service import notify
    from app.memory.service import MemoryService

    commission.status = "completed"
    commission.completed_at = datetime.now(UTC)
    await db.commit()

    user_id = commission.acceptor_user_id
    if not user_id:
        return

    await reward(db, user_id, commission.reward_sc, f"commission:{commission.id}")

    svc = MemoryService(db)
    await svc.add_memory(
        commission.issuer_resident_id, "event", f"有人帮我完成了「{commission.title}」",
        importance=0.7, source="commission", related_user_id=user_id,
    )
    target_slug = (commission.payload_json or {}).get("target_slug")
    if target_slug:
        target = (await db.execute(select(Resident).where(Resident.slug == target_slug))).scalar_one_or_none()
        if target and target.id != commission.issuer_resident_id:
            await svc.add_memory(
                target.id, "event", "有人帮别人给我带了话",
                importance=0.7, source="commission", related_user_id=user_id,
            )

    await notify(db, user_id, "commission", "委托完成", f"你完成了「{commission.title}」", {"reward_sc": commission.reward_sc})
    await emit(db, "commission_completed", user_id=user_id, commission_id=commission.id)


async def expire_commissions(db) -> int:
    """Flip open/accepted commissions past their expiry to 'expired'. Returns count."""
    now = datetime.now(UTC)
    result = await db.execute(
        update(Commission)
        .where(Commission.status.in_(["open", "accepted"]), Commission.expires_at <= now)
        .values(status="expired")
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount or 0


def _keyword_hit_ratio(message: str, said: str) -> float:
    tokens = [t for t in re.split(r"[\s,，、。.！!？?]+", message) if t]
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in said)
    return hits / len(tokens)


# ── Completion detection ─────────────────────────────────────────────

@on("chat_completed")
async def _commission_on_chat(db, user_id: str = "", resident_id: str | None = None,
                              turns: int = 0, conversation_id: str | None = None, **kw) -> None:
    if not user_id or not resident_id:
        return
    async with async_session() as s:
        resident = await s.get(Resident, resident_id)
        if resident is None:
            return
        comms = (await s.execute(
            select(Commission).where(
                Commission.acceptor_user_id == user_id,
                Commission.status == "accepted",
                Commission.kind.in_(["deliver_message", "chat_topic"]),
            )
        )).scalars().all()
        for c in comms:
            payload = c.payload_json or {}
            if payload.get("target_slug") != resident.slug:
                continue
            if c.kind == "chat_topic":
                if turns >= int(payload.get("min_turns", 3)):
                    await complete(s, c)
            elif c.kind == "deliver_message":
                said = ""
                if conversation_id:
                    rows = (await s.execute(
                        select(Message.content).where(
                            Message.conversation_id == conversation_id, Message.role == "user",
                        )
                    )).scalars().all()
                    said = " ".join(rows)
                if _keyword_hit_ratio(payload.get("message", ""), said) >= 0.6:
                    await complete(s, c)


async def check_visit_commissions(db, user_id: str, location_id: str) -> None:
    """Called from the LocationTracker enter path for visit_location commissions."""
    comms = (await db.execute(
        select(Commission).where(
            Commission.acceptor_user_id == user_id,
            Commission.status == "accepted",
            Commission.kind == "visit_location",
        )
    )).scalars().all()
    for c in comms:
        if (c.payload_json or {}).get("location_id") == location_id:
            await complete(db, c)
