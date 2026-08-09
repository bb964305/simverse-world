"""C3 season polls: list open polls, cast a vote."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.rate_limit import limiter
from app.services.auth_service import get_current_user
from app.services.script_service import open_polls, cast_vote, PollError

router = APIRouter(prefix="/polls", tags=["polls"])

# ``topic`` / ``label`` are player free text behind nothing but a Bearer token,
# and they do NOT stop at the polls table: town_facts_service feeds them into
# every NPC's system prompt and decision prompt, and civic_service's clerk
# announcement broadcasts them as a *persistent memory* for all 14 residents —
# once written, they cannot be taken back. The read side clips them as a
# backstop; this is the "should never have been accepted" gate.
#
# TOPIC_MAX_CHARS matches Poll.question's String(300) exactly: anything wider is
# just deferring the rejection to a Postgres DataError.
TOPIC_MAX_CHARS = 300
OPTION_LABEL_MAX_CHARS = 60
#: A poll people are meant to choose from, not a phone book.
OPTIONS_MAX = 20


class VoteBody(BaseModel):
    option_idx: int


class ProposeOption(BaseModel):
    label: str = Field(max_length=OPTION_LABEL_MAX_CHARS)
    effect: dict | None = None


class ProposeBody(BaseModel):
    topic: str = Field(max_length=TOPIC_MAX_CHARS)
    options: list[ProposeOption] = Field(max_length=OPTIONS_MAX)
    days: int | None = None


@router.get("/open")
async def list_open_polls(request: Request, season_id: str | None = None, db: AsyncSession = Depends(get_db)):
    # Auth is optional here: with a valid token each poll carries my_vote so
    # the UI can restore the voted state across reloads.
    user_id = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        user = await get_current_user(db, auth.removeprefix("Bearer "))
        user_id = user.id if user else None
    return {"polls": await open_polls(db, season_id, user_id=user_id)}


@router.post("/propose")
@limiter.limit(lambda: f"{settings.rest_rate_limit_propose_per_minute}/minute")
async def propose_poll(body: ProposeBody, request: Request, db: AsyncSession = Depends(get_db)):
    """M3 F3.1: open a civic proposal poll. Requires auth; admins only may
    attach a landing ``effect`` (system_config / dynamic_location / narrative)
    — a non-admin's options have their effect stripped (advisory poll).

    Rate-limited by IP: ``app.rate_limit`` has ``default_limits=[]``, so a route
    without an explicit ``@limiter.limit`` is not limited at all. Opening a poll
    is a write plus a town-wide broadcast — a script could run it all night.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    if len(body.options) < 2:
        raise HTTPException(status_code=400, detail="need at least 2 options")

    is_admin = bool(getattr(user, "is_admin", False))
    options = [
        {"label": o.label, "effect": (o.effect if is_admin else None)}
        for o in body.options
    ]
    from app.services.civic_service import propose
    poll = await propose(db, body.topic, options, days=body.days)
    if poll is None:
        raise HTTPException(status_code=403, detail="civic polls are disabled")
    return {"ok": True, "poll_id": poll.id}


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
