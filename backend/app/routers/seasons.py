"""Season endpoints (E12 leaderboard; C3 polls added later)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_current_user
from app.services.season_service import get_active_season, leaderboard

router = APIRouter(prefix="/seasons", tags=["seasons"])


@router.get("/current")
async def current_season(db: AsyncSession = Depends(get_db)):
    season = await get_active_season(db)
    if not season:
        return {"season": None}
    return {"season": {"id": season.id, "title": season.title, "theme": season.theme,
                       "status": season.status, "ends_at": season.ends_at.isoformat() if season.ends_at else None}}


@router.get("/current/leaderboard")
async def current_leaderboard(request: Request, around_me: bool = False, db: AsyncSession = Depends(get_db)):
    season = await get_active_season(db)
    if not season:
        # No active season is a normal state, not an error: return an empty
        # board (P2 fix — the 404 forced clients to special-case it).
        return {"top": [], "season": None}
    user_id = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        user = await get_current_user(db, auth.removeprefix("Bearer "))
        user_id = user.id if user else None
    return await leaderboard(db, season.id, user_id=user_id, around_me=around_me)
