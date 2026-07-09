"""Feed + follow endpoints (E11)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_current_user
from app.services.feed_service import list_feed, follow, unfollow, FeedError

router = APIRouter(tags=["feed"])


async def _require_user(request: Request, db: AsyncSession):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return user


@router.get("/feed")
async def get_feed(request: Request, cursor: str | None = None, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    return await list_feed(db, user.id, cursor)


@router.post("/follows/{resident_slug}")
async def add_follow(resident_slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    try:
        await follow(db, user.id, resident_slug)
    except FeedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.delete("/follows/{resident_slug}")
async def remove_follow(resident_slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    await unfollow(db, user.id, resident_slug)
    return {"ok": True}
