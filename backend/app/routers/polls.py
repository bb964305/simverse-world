"""C3 season polls: list open polls, cast a vote."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_current_user
from app.services.script_service import open_polls, cast_vote, PollError

router = APIRouter(prefix="/polls", tags=["polls"])


class VoteBody(BaseModel):
    option_idx: int


@router.get("/open")
async def list_open_polls(season_id: str | None = None, db: AsyncSession = Depends(get_db)):
    return {"polls": await open_polls(db, season_id)}


@router.post("/{poll_id}/vote")
async def vote_poll(poll_id: str, body: VoteBody, request: Request, db: AsyncSession = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    try:
        await cast_vote(db, poll_id, user.id, body.option_idx)
    except PollError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}
