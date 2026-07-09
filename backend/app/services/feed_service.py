"""E11 follow feed: push events, cursor-paginated read, follow/unfollow.

`push` is the one-liner other features call when a resident does something notable
(goal milestone, creation, personality shift, mood swing, debate). It writes a
FeedEvent (own session, decoupled) and live-pushes to any online follower.
"""

import logging
from datetime import datetime, UTC

from sqlalchemy import select, delete, func

from app.database import async_session
from app.models.feed import Follow, FeedEvent
from app.models.resident import Resident
from app.ws.manager import manager

logger = logging.getLogger(__name__)

FOLLOW_CAP = 50
PAGE_SIZE = 20


class FeedError(Exception):
    """Raised for follow errors (router maps to 400)."""


async def push(resident_slug: str, kind: str, payload: dict | None = None) -> None:
    """Record a feed event and live-push it to online followers (best-effort)."""
    try:
        async with async_session() as db:
            event = FeedEvent(resident_slug=resident_slug, kind=kind, payload_json=payload or {})
            db.add(event)
            await db.commit()
            followers = (await db.execute(
                select(Follow.user_id).where(Follow.resident_slug == resident_slug)
            )).scalars().all()
        for uid in followers:
            try:
                if await manager.is_online(uid):
                    await manager.send(uid, {
                        "type": "feed_event", "resident_slug": resident_slug,
                        "kind": kind, "payload": payload or {},
                    })
            except Exception:
                pass
    except Exception:
        logger.warning("feed push failed for %s/%s", resident_slug, kind, exc_info=True)


async def follow(db, user_id: str, resident_slug: str) -> None:
    resident = (await db.execute(select(Resident).where(Resident.slug == resident_slug))).scalar_one_or_none()
    if resident is None:
        raise FeedError("resident not found")
    count = (await db.execute(
        select(func.count()).select_from(Follow).where(Follow.user_id == user_id)
    )).scalar() or 0
    existing = (await db.execute(
        select(Follow).where(Follow.user_id == user_id, Follow.resident_slug == resident_slug)
    )).scalar_one_or_none()
    if existing is not None:
        return
    if count >= FOLLOW_CAP:
        raise FeedError(f"follow limit reached ({FOLLOW_CAP})")
    db.add(Follow(user_id=user_id, resident_slug=resident_slug))
    await db.commit()


async def unfollow(db, user_id: str, resident_slug: str) -> None:
    await db.execute(delete(Follow).where(
        Follow.user_id == user_id, Follow.resident_slug == resident_slug,
    ))
    await db.commit()


def _serialize(e: FeedEvent) -> dict:
    return {
        "id": e.id,
        "resident_slug": e.resident_slug,
        "kind": e.kind,
        "payload": e.payload_json or {},
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


async def list_feed(db, user_id: str, cursor: str | None = None) -> dict:
    followed = (await db.execute(
        select(Follow.resident_slug).where(Follow.user_id == user_id)
    )).scalars().all()
    if not followed:
        return {"events": [], "next_cursor": None}

    q = select(FeedEvent).where(FeedEvent.resident_slug.in_(followed))
    if cursor:
        try:
            ts_str, cid = cursor.split("|", 1)
            ts = datetime.fromisoformat(ts_str)
            q = q.where(
                (FeedEvent.created_at < ts) |
                ((FeedEvent.created_at == ts) & (FeedEvent.id < cid))
            )
        except ValueError:
            pass
    q = q.order_by(FeedEvent.created_at.desc(), FeedEvent.id.desc()).limit(PAGE_SIZE + 1)
    rows = list((await db.execute(q)).scalars().all())
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    next_cursor = f"{rows[-1].created_at.isoformat()}|{rows[-1].id}" if (has_more and rows) else None
    return {"events": [_serialize(e) for e in rows], "next_cursor": next_cursor}
