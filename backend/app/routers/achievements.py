"""Achievements endpoint (S2): definitions merged with the caller's progress."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.events.achievements import get_user_achievements
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/achievements", tags=["achievements"])


@router.get("")
async def list_achievements(request: Request, db: AsyncSession = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return {"achievements": await get_user_achievements(db, user.id)}
