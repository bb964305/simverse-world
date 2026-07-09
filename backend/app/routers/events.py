"""Public world-event endpoints (S1)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.world_event_service import get_active_events_cached

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/active")
async def active_events(db: AsyncSession = Depends(get_db)):
    """Currently active world events (60s-cached snapshot)."""
    return {"events": await get_active_events_cached(db)}
