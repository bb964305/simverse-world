from datetime import date, datetime, timedelta, UTC

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query
from sqlalchemy import select, desc, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.resident import Resident
from app.models.conversation import Conversation
from app.models.memory import Memory
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.profile import (
    MyResidentItem,
    MyConversationItem,
    MyTransactionItem,
    CreatorResidentStats,
    CreatorStatsResponse,
    CreatorTotals,
    SeriesPoint,
    WeeklyRatingPoint,
)
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/profile", tags=["profile"])

# D4 creator dashboard lives at /creator/stats (spec path), but shares this
# module's auth helper; registered separately in app.main.
creator_router = APIRouter(prefix="/creator", tags=["creator"])


async def _require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


@router.get("/residents", response_model=list[MyResidentItem])
async def list_my_residents(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user(request, db)
    result = await db.execute(
        select(Resident)
        .where(Resident.creator_id == user.id)
        .order_by(desc(Resident.created_at))
        # P1-3 audit: per-user growth is slow (creation capped at 3/day) but
        # unbounded over time; safety cap far above any realistic roster.
        .limit(500)
    )
    residents = result.scalars().all()
    return [MyResidentItem.model_validate(r, from_attributes=True) for r in residents]


@router.get("/conversations", response_model=list[MyConversationItem])
async def list_my_conversations(
    request: Request,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user(request, db)
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(desc(Conversation.started_at))
        .limit(limit)
        .offset(offset)
    )
    conversations = result.scalars().all()

    items = []
    for conv in conversations:
        res_result = await db.execute(
            select(Resident.name, Resident.slug).where(Resident.id == conv.resident_id)
        )
        row = res_result.first()
        resident_name = row[0] if row else "Unknown"
        resident_slug = row[1] if row else ""
        items.append(MyConversationItem(
            id=conv.id,
            resident_id=conv.resident_id,
            resident_name=resident_name,
            resident_slug=resident_slug,
            started_at=conv.started_at,
            ended_at=conv.ended_at,
            turns=conv.turns,
            rating=conv.rating,
        ))
    return items


@router.get("/transactions", response_model=list[MyTransactionItem])
async def list_my_transactions(
    request: Request,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user(request, db)
    result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .order_by(desc(Transaction.created_at))
        .limit(limit)
        .offset(offset)
    )
    transactions = result.scalars().all()
    return [MyTransactionItem(
        id=t.id, amount=t.amount, reason=t.reason, created_at=t.created_at
    ) for t in transactions]


# ── D4 Creator dashboard ────────────────────────────────────────────────────

_STATS_WINDOW_DAYS = 30

# Creator income ledger reasons (see FEATURE_SPECS §D4). The spec names
# 'creator_passive%' and 'purchase:tip%'; the actual creator-side tip income
# is written as 'tip_share:{post_id}' (shop_effects.py), so it is included
# too. The `amount > 0` guard below keeps buyer-side rows (e.g. the creator's
# own 'purchase:tip_5sc' spend, negative) out of the earnings series.
_EARNING_REASON_PATTERNS = ("creator_passive%", "purchase:tip%", "tip_share:%")


def _week_start(d: date) -> date:
    """Monday of the ISO week containing d."""
    return d - timedelta(days=d.weekday())


@creator_router.get("/stats", response_model=CreatorStatsResponse)
async def creator_stats(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Aggregated 30-day stats for all residents created by the current user.

    Single endpoint returning every series at once (data volume is small).
    All bucketing SQL sticks to func.date()/case-free sums so it runs
    identically on sqlite (dev/tests) and PostgreSQL (prod); weekly rating
    buckets are folded in Python to avoid strftime/date_trunc dialect splits.
    """
    user = await _require_user(request, db)
    response.headers["Cache-Control"] = "max-age=300"

    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=_STATS_WINDOW_DAYS - 1)
    # Window lower bound as an aware datetime for timestamp comparisons.
    since = datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC)

    residents = (await db.execute(
        select(Resident)
        .where(Resident.creator_id == user.id)
        .order_by(desc(Resident.created_at))
        .limit(500)
    )).scalars().all()

    if not residents:
        # Empty structure → frontend renders the "go create one" guidance state.
        return CreatorStatsResponse(
            window_days=_STATS_WINDOW_DAYS,
            since=start_day.isoformat(),
            residents=[],
            daily_conversations=[],
            daily_earnings=[],
            weekly_ratings=[],
            totals=CreatorTotals(conversations=0, earnings_sc=0, memories=0, avg_rating=None),
        )

    ids = [r.id for r in residents]
    slug_to_id = {r.slug: r.id for r in residents}

    # 1) Conversations per resident per day. func.date() renders DATE(...) on
    #    both sqlite and PG (same pattern as admin/economy.py's series).
    conv_day = func.date(Conversation.started_at)
    conv_rows = (await db.execute(
        select(Conversation.resident_id, conv_day, func.count(Conversation.id))
        .where(Conversation.resident_id.in_(ids), Conversation.started_at >= since)
        .group_by(Conversation.resident_id, conv_day)
    )).all()
    conv_by_resident: dict[str, dict[str, int]] = {}
    conv_total_by_day: dict[str, int] = {}
    for rid, d, n in conv_rows:
        day, n = str(d), int(n or 0)
        conv_by_resident.setdefault(rid, {})[day] = n
        conv_total_by_day[day] = conv_total_by_day.get(day, 0) + n

    # 2) Ratings per resident per day (sum + count); weekly folding in Python.
    rating_rows = (await db.execute(
        select(
            Conversation.resident_id,
            conv_day,
            func.sum(Conversation.rating),
            func.count(Conversation.rating),
        )
        .where(
            Conversation.resident_id.in_(ids),
            Conversation.started_at >= since,
            Conversation.rating.is_not(None),
        )
        .group_by(Conversation.resident_id, conv_day)
    )).all()
    rating_sum_by_resident: dict[str, list[int]] = {}  # rid -> [sum, count]
    rating_by_week: dict[str, list[int]] = {}          # week_start -> [sum, count]
    for rid, d, rsum, rcount in rating_rows:
        rsum, rcount = int(rsum or 0), int(rcount or 0)
        acc = rating_sum_by_resident.setdefault(rid, [0, 0])
        acc[0] += rsum
        acc[1] += rcount
        wk = _week_start(date.fromisoformat(str(d))).isoformat()
        wacc = rating_by_week.setdefault(wk, [0, 0])
        wacc[0] += rsum
        wacc[1] += rcount

    # 3) SC earnings per day, kept at (day, reason) grain so creator_passive
    #    rows ('creator_passive:{slug}') can be attributed to residents.
    tx_day = func.date(Transaction.created_at)
    earn_rows = (await db.execute(
        select(tx_day, Transaction.reason, func.sum(Transaction.amount))
        .where(
            Transaction.user_id == user.id,
            Transaction.created_at >= since,
            Transaction.amount > 0,
            or_(*(Transaction.reason.like(p) for p in _EARNING_REASON_PATTERNS)),
        )
        .group_by(tx_day, Transaction.reason)
    )).all()
    earn_by_day: dict[str, int] = {}
    earn_by_resident: dict[str, int] = {}
    for d, reason, amount in earn_rows:
        day, amount = str(d), int(amount or 0)
        earn_by_day[day] = earn_by_day.get(day, 0) + amount
        if reason.startswith("creator_passive:"):
            rid = slug_to_id.get(reason.removeprefix("creator_passive:"))
            if rid:
                earn_by_resident[rid] = earn_by_resident.get(rid, 0) + amount

    # 4) Memory footprint: memories held by the creator's residents (30d).
    mem_rows = (await db.execute(
        select(Memory.resident_id, func.count(Memory.id))
        .where(Memory.resident_id.in_(ids), Memory.created_at >= since)
        .group_by(Memory.resident_id)
    )).all()
    mem_by_resident = {rid: int(n or 0) for rid, n in mem_rows}

    # ── Assemble ────────────────────────────────────────────────────────────
    window = [(start_day + timedelta(days=i)).isoformat() for i in range(_STATS_WINDOW_DAYS)]

    resident_stats: list[CreatorResidentStats] = []
    for r in residents:
        daily = conv_by_resident.get(r.id, {})
        rsum, rcount = rating_sum_by_resident.get(r.id, [0, 0])
        resident_stats.append(CreatorResidentStats(
            id=r.id,
            slug=r.slug,
            name=r.name,
            sprite_key=r.sprite_key,
            star_rating=r.star_rating,
            conversations_30d=sum(daily.values()),
            avg_rating_30d=round(rsum / rcount, 2) if rcount else None,
            earnings_30d=earn_by_resident.get(r.id, 0),
            memories_30d=mem_by_resident.get(r.id, 0),
            daily_conversations=[
                SeriesPoint(date=d, value=n) for d, n in sorted(daily.items())
            ],
        ))

    weekly_ratings: list[WeeklyRatingPoint] = []
    wk = _week_start(start_day)
    while wk <= today:
        wsum, wcount = rating_by_week.get(wk.isoformat(), [0, 0])
        weekly_ratings.append(WeeklyRatingPoint(
            week_start=wk.isoformat(),
            avg_rating=round(wsum / wcount, 2) if wcount else None,
            count=wcount,
        ))
        wk += timedelta(days=7)

    total_rsum = sum(acc[0] for acc in rating_sum_by_resident.values())
    total_rcount = sum(acc[1] for acc in rating_sum_by_resident.values())
    return CreatorStatsResponse(
        window_days=_STATS_WINDOW_DAYS,
        since=start_day.isoformat(),
        residents=resident_stats,
        daily_conversations=[
            SeriesPoint(date=d, value=conv_total_by_day.get(d, 0)) for d in window
        ],
        daily_earnings=[
            SeriesPoint(date=d, value=earn_by_day.get(d, 0)) for d in window
        ],
        weekly_ratings=weekly_ratings,
        totals=CreatorTotals(
            conversations=sum(conv_total_by_day.values()),
            earnings_sc=sum(earn_by_day.values()),
            memories=sum(mem_by_resident.values()),
            avg_rating=round(total_rsum / total_rcount, 2) if total_rcount else None,
        ),
    )
