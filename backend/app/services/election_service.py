"""Seasonal mayor election (M6) — built on the M3 civic-poll engine.

An election is a civic poll whose options are candidate residents; each option
carries a ``{"type": "mayor", "slug": ...}`` effect. Voting (NPC rule-based +
player) and closing reuse ``civic_service`` verbatim. When the poll closes the
winner is installed as mayor:

  - ``meta_json['mayor'] = True`` on the winner (and cleared on everyone else),
    which the wage path reads for the town-wide bonus — no extra query;
  - ``current_mayor`` recorded in system_config for provenance;
  - the clerk posts the result, and the winner's bulletins carry a mayor badge
    implicitly via authorship.

Gated by ``settings.election_enabled``. Fail-open.
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.resident import Resident

logger = logging.getLogger(__name__)

ELECTION_TAG = "镇长选举"


async def open_election(db, *, candidate_slugs: list[str] | None = None, days: int | None = None):
    """Open a mayor election poll. Candidates default to ambitious, socially
    active NPCs (SBTI Ac1=H or So1=H). Returns the Poll or None."""
    if not settings.election_enabled:
        return None
    from app.services import civic_service

    residents = (await db.execute(
        select(Resident).where(Resident.resident_type == "npc")
    )).scalars().all()
    by_slug = {r.slug: r for r in residents}

    if candidate_slugs:
        candidates = [by_slug[s] for s in candidate_slugs if s in by_slug]
    else:
        candidates = [
            r for r in residents
            if _dim(r, "Ac1") == "H" or _dim(r, "So1") == "H"
        ]
        if len(candidates) < 2:  # fallback: highest-heat residents
            candidates = sorted(residents, key=lambda r: r.heat or 0, reverse=True)[:3]
    if settings.rep_enabled:
        from app.services.reputation_service import score_from_meta
        # Stable sort: equal reputation preserves the existing SBTI/heat order.
        candidates = sorted(
            candidates,
            key=lambda resident: score_from_meta(resident.meta_json),
            reverse=True,
        )
    candidates = candidates[:4]
    if len(candidates) < 2:
        return None

    options = [
        {"label": c.name, "effect": {"type": "mayor", "slug": c.slug}}
        for c in candidates
    ]
    return await civic_service.propose(
        db, f"{ELECTION_TAG}:谁来当下一任镇长?", options, days=days,
    )


async def maybe_open_seasonal_election(db):
    """Nightly trigger (M6): open a mayor election when one is due.

    Cadence rules (all state in system_config — survives restarts, no schema):
      - never while an election poll is already open;
      - a season becoming active holds an election once per season
        (``election_last_season`` remembers the season already served);
      - off-season, elections repeat every ``election_interval_days``
        (``election_last_opened`` stores the last open date).

    Returns the opened Poll or None. Fail-open: any error means "not tonight".
    """
    if not (settings.election_enabled and settings.civic_polls_enabled):
        return None
    from app.models.season import Poll
    open_poll = (await db.execute(
        select(Poll).where(
            Poll.status == "open", Poll.question.like(f"{ELECTION_TAG}%"),
        )
    )).scalars().first()
    if open_poll is not None:
        return None

    from app.services.config_service import ConfigService
    cs = ConfigService(db)

    season = None
    try:
        from app.services.season_service import get_active_season
        season = await get_active_season(db)
    except Exception:
        logger.warning("active-season lookup failed for election", exc_info=True)

    if season is not None:
        if await cs.get("election_last_season") == season.id:
            return None
        poll = await open_election(db)
        if poll is not None:
            await cs.set("election_last_season", season.id,
                         group="civic", updated_by="election")
            await cs.set("election_last_opened", datetime.now(UTC).date().isoformat(),
                         group="civic", updated_by="election")
        return poll

    today = datetime.now(UTC).date()
    last = await cs.get("election_last_opened")
    if last:
        try:
            from datetime import date as _date
            elapsed = (today - _date.fromisoformat(str(last))).days
            if elapsed < settings.election_interval_days:
                return None
        except ValueError:
            logger.warning("unparseable election_last_opened %r — reopening", last)
    poll = await open_election(db)
    if poll is not None:
        await cs.set("election_last_opened", today.isoformat(),
                     group="civic", updated_by="election")
    return poll


async def install_mayor(db, slug: str | None) -> bool:
    """Set ``slug`` as the sitting mayor (clearing any previous one) and record
    it in system_config. Returns True on success."""
    if not slug:
        return False
    residents = (await db.execute(
        select(Resident).where(Resident.resident_type == "npc")
    )).scalars().all()
    winner = None
    for r in residents:
        meta = dict(r.meta_json or {})
        was = bool(meta.get("mayor"))
        should = (r.slug == slug)
        if was != should:
            if should:
                meta["mayor"] = True
                winner = r
            else:
                meta.pop("mayor", None)
            r.meta_json = meta
            flag_modified(r, "meta_json")
        elif should:
            winner = r
    await db.commit()

    if winner is None:
        return False
    try:
        from app.services.config_service import ConfigService
        await ConfigService(db).set(
            "current_mayor", slug, group="civic", updated_by="election",
        )
    except Exception:
        logger.warning("recording current_mayor failed", exc_info=True)
    # S2-1: dual-write the offices row when the gate is on. Both legacy
    # stores above stay alive — meta_json['mayor'] is the wage multiplier
    # (gotcha #1), system_config the read fallback. Fail-open: an offices
    # hiccup must never break an election result.
    if settings.polis_office_enabled:
        try:
            from app.services.office_service import OfficeService
            await OfficeService(db).appoint(
                "mayor", slug, fill_strategy="election",
                term_days=settings.polis_office_mayor_term_days,
            )
        except Exception:
            logger.warning("office dual-write failed for mayor", exc_info=True)
    try:
        from app.services.feed_service import push
        await push(slug, "goal_achieved", {"goal": "当选小镇镇长"})
        from app.memory.service import MemoryService
        await MemoryService(db).add_memory(
            winner.id, "event",
            "我当选了小镇的镇长。这份信任沉甸甸的,得对得起投我票的每一个人。",
            0.9, "reflection",
        )
    except Exception:
        logger.warning("mayor install side-effects failed", exc_info=True)
    return True


async def current_mayor(db) -> str | None:
    # S2-1: offices-backed read when the gate is on — offices is the new
    # authority, system_config the fallback (pre-backfill worlds, or a
    # vacant office after term expiry with a legacy value already cleared
    # by term_check). Gate off → byte-level legacy behavior.
    if settings.polis_office_enabled:
        try:
            from app.services.office_service import OfficeService
            holder = await OfficeService(db).get_holder("mayor")
            if holder:
                return holder
        except Exception:
            logger.warning("offices-backed current_mayor read failed", exc_info=True)
    from app.services.config_service import ConfigService
    try:
        return await ConfigService(db).get("current_mayor")
    except Exception:
        return None


def _dim(resident, code: str) -> str:
    return (resident.meta_json or {}).get("sbti", {}).get("dimensions", {}).get(code, "M")
