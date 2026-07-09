"""Daily loop endpoints (D3)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_current_user
from app.services.daily_quest_service import get_today_quest, serialize

router = APIRouter(prefix="/daily", tags=["daily"])


@router.get("/quest")
async def daily_quest(request: Request, db: AsyncSession = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    q = await get_today_quest(db, user.id)
    return {
        "quest": serialize(q) if q else None,
        "login_streak": user.login_streak,
    }
