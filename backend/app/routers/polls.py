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

#: 投票窗口的上下界(**真实**天)。``days`` 隔着的也只有一个 Bearer token,而它比
#: 自由文本更硬 —— 两个坏值形状不同:
#:
#: - 约 291.5 万天之后 ``datetime.now(UTC) + timedelta(days=...)``
#:   (``civic_service.propose``)直接 ``OverflowError``,那是一个 500。
#: - 够不着溢出的大值更阴:40 万天不抛异常,悄悄开出一张 3121 年才截止的公投,
#:   ``close_due_polls`` 永远等不到它,而开票公告早已作为**持久记忆**发给全镇了。
#:
#: 上界取一个自然月:公投是「镇上正在议的事」,议一个月还没结的不是公投。默认
#: ``world_clock_k=4`` 下 30 真实日 = 120 世界日,超过 ``POLL_CLOSES_IN_MAX_DAYS``
#: (99),所以顶格窗口的公告会说「还有 99 天以上截止」—— 中文的「以上」含本数,
#: 这句话仍为真。下界是 1:``days<=0`` 开出来的票当场就已经过期。
POLL_DAYS_MIN = 1
POLL_DAYS_MAX = 30


class VoteBody(BaseModel):
    option_idx: int


class ProposeOption(BaseModel):
    label: str = Field(max_length=OPTION_LABEL_MAX_CHARS)
    effect: dict | None = None


class ProposeBody(BaseModel):
    topic: str = Field(max_length=TOPIC_MAX_CHARS)
    options: list[ProposeOption] = Field(max_length=OPTIONS_MAX)
    days: int | None = Field(default=None, ge=POLL_DAYS_MIN, le=POLL_DAYS_MAX)


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
