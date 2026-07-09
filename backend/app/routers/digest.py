"""Digest endpoints (A5)."""

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Request

from app.database import get_db
from app.models.digest import Digest
from app.services.auth_service import get_current_user
from app.services.digest_service import serialize, generate_weekly_recap

router = APIRouter(prefix="/digest", tags=["digest"])


@router.get("/weekly/me")
async def weekly_recap(request: Request, db: AsyncSession = Depends(get_db)):
    """E14: this week's personal recap (lazily generated, idempotent)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    d = await generate_weekly_recap(db, user.id)
    return {"digest": serialize(d)}


@router.get("/latest")
async def latest(db: AsyncSession = Depends(get_db)):
    d = (await db.execute(
        select(Digest).where(Digest.scope == "village", Digest.user_id == "")
        .order_by(Digest.date.desc()).limit(1)
    )).scalar_one_or_none()
    return {"digest": serialize(d) if d else None}


@router.get("")
async def by_date(date: str, db: AsyncSession = Depends(get_db)):
    try:
        day = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    d = (await db.execute(
        select(Digest).where(Digest.scope == "village", Digest.user_id == "", Digest.date == day)
    )).scalar_one_or_none()
    return {"digest": serialize(d) if d else None}
