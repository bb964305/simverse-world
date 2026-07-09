"""Notification service (S4).

`notify` writes a durable row (committing its own write, matching coin_service's
convention) and, if the user is currently online, pushes a live WS notification.
The WS push is best-effort — a socket hiccup must never fail the notify call.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.ws.manager import manager

logger = logging.getLogger(__name__)

VALID_KINDS = {
    "resident_greeting", "achievement", "capsule_delivered",
    "commission", "feed", "system",
}


def serialize(n: Notification) -> dict:
    return {
        "id": n.id,
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "payload": n.payload_json or {},
        "read": n.read_at is not None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


async def notify(
    db: AsyncSession,
    user_id: str,
    kind: str,
    title: str,
    body: str = "",
    payload: dict | None = None,
) -> Notification:
    """Persist a notification and, if the user is online, push it over WS."""
    n = Notification(
        user_id=user_id, kind=kind, title=title, body=body, payload_json=payload or {},
    )
    db.add(n)
    await db.commit()
    await db.refresh(n)

    try:
        if await manager.is_online(user_id):
            await manager.send(user_id, {"type": "notification", **serialize(n)})
    except Exception:
        logger.warning("notification WS push failed for user %s", user_id, exc_info=True)

    return n
