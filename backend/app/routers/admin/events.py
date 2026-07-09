"""Admin world-event management (S1) — require_admin on all routes."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.world_event import WorldEvent
from app.routers.admin.middleware import require_admin
from app.services.world_event_service import invalidate_active_cache

router = APIRouter(prefix="/events", tags=["admin-events"])


class EventCreate(BaseModel):
    type: str = "custom"
    title: str
    description: str = ""
    payload_json: dict = {}
    starts_at: datetime
    ends_at: datetime


class EventUpdate(BaseModel):
    type: str | None = None
    title: str | None = None
    description: str | None = None
    payload_json: dict | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


def _serialize(e: WorldEvent) -> dict:
    return {
        "id": e.id,
        "type": e.type,
        "title": e.title,
        "description": e.description,
        "payload_json": e.payload_json or {},
        "starts_at": e.starts_at.isoformat() if e.starts_at else None,
        "ends_at": e.ends_at.isoformat() if e.ends_at else None,
        "is_active": e.is_active,
        "created_by": e.created_by,
    }


@router.get("")
async def list_events(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(WorldEvent).order_by(WorldEvent.starts_at.desc()))
    return {"events": [_serialize(e) for e in result.scalars().all()]}


@router.post("")
async def create_event(
    body: EventCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    if body.ends_at <= body.starts_at:
        raise HTTPException(status_code=400, detail="ends_at must be after starts_at")
    # Created inactive; the cron flips + broadcasts the start transition so the
    # is_active edge (and the WS broadcast) always flows through one code path.
    event = WorldEvent(
        type=body.type,
        title=body.title.strip(),
        description=body.description,
        payload_json=body.payload_json,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        created_by=admin.id,
        is_active=False,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return _serialize(event)


@router.patch("/{event_id}")
async def update_event(
    event_id: str,
    body: EventUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    event = await db.get(WorldEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(event, field, value)
    await db.commit()
    await db.refresh(event)
    invalidate_active_cache()
    return _serialize(event)


@router.delete("/{event_id}")
async def delete_event(
    event_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    event = await db.get(WorldEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await db.delete(event)
    await db.commit()
    invalidate_active_cache()
    return {"ok": True}
