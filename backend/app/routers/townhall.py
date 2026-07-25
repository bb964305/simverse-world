"""Read-only 市政厅 (town hall) aggregation router — Society Expansion §10.

A thin read-only projection over existing M1–M6 data so the political layer is
visible early, before any policies/offices/treasury tables exist:

  GET /townhall/overview     sitting mayor, duty holders, open civic proposals,
                             most recent mayor-election result, config-projected
                             town finances.
  GET /townhall/market-day   whether a 集市日 is active + the shop discount rate,
                             read by the shop catalog to show折后价 labels.

Every branch is best-effort / fail-open: a broken sub-query degrades to an empty
section rather than a 500 — this endpoint only ever reads.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.auth_service import get_current_user
from app.models.resident import Resident
from app.models.season import Poll
from app.services import duty_service, election_service, script_service
from app.services import world_event_service
from app.services.config_service import ConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/townhall", tags=["townhall"])

# 收口 (2026-07-25B): SOCIETY_EXPANSION_PLAN §6 names the two player-read-only
# civic endpoints ``GET /town/treasury`` (S1-5) and ``GET /town/policies``
# (S2-5). Both landed on /townhall because the parallel lines could not edit
# main.py; this alias router serves the spec paths from the same handlers.
alias_router = APIRouter(prefix="/town", tags=["townhall"])

# Finance config keys projected verbatim (policies table not built yet, §10).
_FINANCE_KEYS = (
    "npc_default_wage_sc", "npc_meal_cost_sc", "market_day_weekday",
    "market_day_discount", "civic_poll_days", "election_mayor_wage_bonus",
    "election_interval_days",
)


async def _npc_residents(db: AsyncSession) -> list[Resident]:
    return (await db.execute(
        select(Resident).where(
            Resident.resident_type == "npc",
            Resident.meta_json.isnot(None),
        )
    )).scalars().all()


async def _mayor(db: AsyncSession, residents: list[Resident]) -> dict | None:
    slug = await election_service.current_mayor(db)
    if not slug:
        return None
    name = next((r.name for r in residents if r.slug == slug), slug)
    return {"slug": slug, "name": name}


def _duties(residents: list[Resident]) -> list[dict]:
    out = []
    for r in residents:
        duty = duty_service.get_duty(r)
        key = duty.get("key")
        if not key:
            continue
        out.append({
            "key": key,
            "title": duty.get("title", ""),
            "holder_slug": r.slug,
            "holder_name": r.name,
        })
    return out


async def _open_proposals(db: AsyncSession) -> list[dict]:
    polls = await script_service.open_polls(db)
    # Elections ride the same Poll table; the panel lists them separately, so
    # the "proposals" section drops anything tagged as a mayor election.
    return [p for p in polls if not p["question"].startswith(election_service.ELECTION_TAG)]


async def _recent_election(db: AsyncSession) -> dict | None:
    poll = (await db.execute(
        select(Poll).where(
            Poll.status == "closed",
            Poll.question.like(f"{election_service.ELECTION_TAG}%"),
        ).order_by(Poll.closes_at.desc()).limit(1)
    )).scalars().first()
    if poll is None:
        return None
    opts = poll.options_json or []
    winner = next((o for o in opts if o.get("won")), None)
    winner_slug = (winner or {}).get("effect", {}).get("slug") if winner else None
    return {
        "question": poll.question,
        "closed_at": poll.closes_at.isoformat() if poll.closes_at else None,
        "winner_slug": winner_slug,
        "winner_name": (winner or {}).get("label") if winner else None,
        "winner_votes": (winner or {}).get("final_votes") if winner else None,
        "options": opts,
    }


async def _finances(db: AsyncSession) -> dict:
    """Config-projected town finances. DB config overrides the settings default
    (a civic vote can land a system_config change), else the compiled default."""
    cs = ConfigService(db)
    out = {}
    for key in _FINANCE_KEYS:
        out[key] = await cs.get(key, default=getattr(settings, key, None))
    return out


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db)):
    residents = await _npc_residents(db)

    mayor = None
    try:
        mayor = await _mayor(db, residents)
    except Exception:
        logger.warning("townhall: mayor lookup failed", exc_info=True)

    try:
        duties = _duties(residents)
    except Exception:
        logger.warning("townhall: duty scan failed", exc_info=True)
        duties = []

    try:
        open_polls = await _open_proposals(db)
    except Exception:
        logger.warning("townhall: open-poll scan failed", exc_info=True)
        open_polls = []

    try:
        recent_election = await _recent_election(db)
    except Exception:
        logger.warning("townhall: recent-election lookup failed", exc_info=True)
        recent_election = None

    try:
        finances = await _finances(db)
    except Exception:
        logger.warning("townhall: finance projection failed", exc_info=True)
        finances = {}

    return {
        "mayor": mayor,
        "duties": duties,
        "open_polls": open_polls,
        "recent_election": recent_election,
        "finances": finances,
    }


@router.get("/market-day")
async def market_day(db: AsyncSession = Depends(get_db)):
    active = False
    try:
        events = await world_event_service.get_active_events_cached(db)
        active = any((e.get("payload_json") or {}).get("market_day") for e in events)
    except Exception:
        logger.warning("townhall: market-day lookup failed", exc_info=True)
    return {
        "active": active,
        "discount": settings.market_day_discount if active else 1.0,
        "weekday": settings.market_day_weekday,
    }


@router.get("/treasury")
@alias_router.get("/treasury")
async def town_treasury(request: Request, db: AsyncSession = Depends(get_db)):
    """S1-5: the town's public account, read-only, for any logged-in player.

    Plain login auth (NOT admin) per SOCIETY_EXPANSION_PLAN §6 — this is a
    civic-transparency projection, not an admin view, and there is deliberately
    no write verb on this path (the treasury only moves through taxes, wages and
    the nightly job). Pure read: it never upserts the town row, so a world with
    the gate off keeps reporting a flat 0.

    DEVIATION (registered in the S1-5 report): the spec names this endpoint
    ``GET /town/treasury``. Mounting a new router requires editing
    ``app/main.py``, which the parallel engineering-health line owns for this
    batch, so it is served from the existing town-hall router instead. The
    handler is path-agnostic — closeout can alias ``/town/treasury`` with a
    one-line include.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")

    balance_sc = 0
    updated_at = None
    try:
        from app.models.town_treasury import TOWN_KEY, TownTreasury
        row = (await db.execute(
            select(TownTreasury).where(TownTreasury.key == TOWN_KEY)
        )).scalar_one_or_none()
        if row is not None:
            balance_sc = row.balance_sc
            updated_at = row.updated_at
    except Exception:
        logger.warning("townhall: treasury lookup failed", exc_info=True)

    return {
        "enabled": settings.town_treasury_enabled,
        "balance_sc": balance_sc,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "tax_rate": settings.town_tax_rate_sales,
    }


@router.get("/policies")
@alias_router.get("/policies")
async def policies(db: AsyncSession = Depends(get_db)):
    """S2-5 玩家只读政策投影 (§6 "GET /town/policies 玩家只读").

    Landed on the existing public read-only ``/townhall`` router rather than a
    new ``/town`` prefix: no ``/town`` router exists in this codebase, and
    ``/townhall`` is exactly the "political layer, read-only, fail-open"
    aggregation surface (see this module's docstring). Deviation recorded in
    docs/reports/feat-s2-5-policies-report.md.

    Gate off → ``{"enabled": false, "policies": []}``: policy state still lives
    in ``system_config`` and the table is not a source of truth yet.
    """
    rows: list[dict] = []
    try:
        from app.services.policy_service import PolicyService, TIER_MATRIX
        rows = await PolicyService(db).list_all()
        matrix = {t: {"path": spec["path"]} for t, spec in TIER_MATRIX.items()}
    except Exception:
        logger.warning("townhall: policy projection failed", exc_info=True)
        matrix = {}
    return {
        "enabled": settings.polis_policy_enabled,
        "tiers": matrix,
        "policies": rows,
    }
