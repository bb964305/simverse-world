"""Notification center endpoints (S4)."""

from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.notification import Notification
from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.notification_service import serialize

router = APIRouter(prefix="/notifications", tags=["notifications"])

PAGE_SIZE = 20


async def _require_user(request: Request, db: AsyncSession) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return user


async def _unread_count(db: AsyncSession, user_id: str) -> int:
    result = await db.execute(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user_id, Notification.read_at.is_(None),
        )
    )
    return int(result.scalar() or 0)


class ReadRequest(BaseModel):
    ids: list[str]


@router.get("")
async def list_notifications(
    request: Request,
    unread_only: bool = False,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user(request, db)
    q = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
            q = q.where(Notification.created_at < cursor_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor")
    q = q.order_by(Notification.created_at.desc()).limit(PAGE_SIZE + 1)

    rows = list((await db.execute(q)).scalars().all())
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    next_cursor = rows[-1].created_at.isoformat() if (has_more and rows) else None

    return {
        "notifications": [serialize(n) for n in rows],
        "unread_count": await _unread_count(db, user.id),
        "next_cursor": next_cursor,
    }


@router.post("/read")
async def mark_read(
    body: ReadRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user(request, db)
    if body.ids:
        await db.execute(
            update(Notification)
            .where(
                Notification.user_id == user.id,
                Notification.id.in_(body.ids),
                Notification.read_at.is_(None),
            )
            .values(read_at=datetime.now(UTC))
        )
        await db.commit()
    return {"unread_count": await _unread_count(db, user.id)}
