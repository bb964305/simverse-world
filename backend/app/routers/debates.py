"""E9 debate arena endpoints: list, stake, vote."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_current_user
from app.models.debate import Debate
from app.services.debate_service import stake, vote, DebateError

router = APIRouter(prefix="/debates", tags=["debates"])


class StakeBody(BaseModel):
    side: str
    amount: int


class VoteBody(BaseModel):
    side: str


async def _auth(request: Request, db: AsyncSession):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return user


def _view(d: Debate) -> dict:
    return {
        "id": d.id, "topic": d.topic, "status": d.status,
        "resident_a_slug": d.resident_a_slug, "resident_b_slug": d.resident_b_slug,
        "pool_a": d.pool_a, "pool_b": d.pool_b,
        "votes_a": d.votes_a, "votes_b": d.votes_b,
        "winner": d.winner, "transcript": d.transcript_json or [],
    }


@router.get("")
async def list_debates(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Debate).order_by(Debate.starts_at.desc()).limit(20))).scalars().all()
    return {"debates": [_view(d) for d in rows]}


@router.get("/{debate_id}")
async def get_debate(debate_id: str, db: AsyncSession = Depends(get_db)):
    d = await db.get(Debate, debate_id)
    if d is None:
        raise HTTPException(status_code=404, detail="no such debate")
    return _view(d)


@router.post("/{debate_id}/stake")
async def stake_debate(debate_id: str, body: StakeBody, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _auth(request, db)
    try:
        s = await stake(db, debate_id, user.id, body.side, body.amount)
    except DebateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": s.id, "side": s.side, "amount": s.amount}


@router.post("/{debate_id}/vote")
async def vote_debate(debate_id: str, body: VoteBody, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _auth(request, db)
    try:
        await vote(db, debate_id, user.id, body.side)
    except DebateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}
