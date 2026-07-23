"""C3 剧本季 (season script): fire scheduled acts, run polls, settle finale.

A season is authored as a row in ``seasons`` plus a set of ``season_scripts``
acts, each with a ``trigger_at`` and an ``event_payload_json`` describing what
that act does. The event cron scans for due acts and, per act:

- opens a world event (``type="script"``) whose description is injected into
  every resident prompt (via the active-event cache),
- posts a public clue to the bulletin board (``kind="clue"``),
- injects private "secret" memories to named residents (``source="script"``)
  so different residents secretly know different things.

Polls let players steer the story: ``open_polls`` lists live polls, ``cast_vote``
records one vote per user (DB-unique). On the season's ``ends_at`` the finale
settles: polls close with a winning option, E12 scores settle, and a finale
recap is posted to the bulletin.
"""

import logging
from datetime import datetime, UTC, timedelta

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.season import Season, SeasonScript, Poll, Vote
from app.models.world_event import WorldEvent
from app.models.bulletin_post import BulletinPost
from app.models.resident import Resident

logger = logging.getLogger(__name__)


async def _resolve_secret_targets(db, slug: str) -> list[Resident]:
    """Resolve a secret's ``resident_slug`` to target residents. P2 §7.2: the
    special form ``circle:<id>`` expands to every resident in that social circle
    (a plot seed festers inside a clique); a plain slug resolves to one resident."""
    if slug.startswith("circle:"):
        from app.services import circle_service
        targets: list[Resident] = []
        for rid in await circle_service.expand_circle(db, slug[len("circle:"):]):
            res = await db.get(Resident, rid)
            if res is not None:
                targets.append(res)
        return targets
    res = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one_or_none()
    return [res] if res is not None else []


class PollError(Exception):
    """Invalid poll/vote request (router maps to 400)."""


# --------------------------------------------------------------------------- #
# Script acts                                                                 #
# --------------------------------------------------------------------------- #
async def fire_due_scripts(db) -> list[dict]:
    """Fire every pending script act whose trigger_at has passed. Idempotent per
    act via the status flip to 'fired'. Returns a summary per fired act."""
    now = datetime.now(UTC)
    scripts = (await db.execute(
        select(SeasonScript).where(SeasonScript.status == "pending").order_by(SeasonScript.trigger_at)
    )).scalars().all()

    fired: list[dict] = []
    for s in scripts:
        trig = s.trigger_at
        if trig is not None and trig.tzinfo is None:
            trig = trig.replace(tzinfo=UTC)
        if trig is None or trig > now:
            continue

        p = s.event_payload_json or {}
        # 1. World event. Realism P0-4: start inactive with starts_at=now so the
        # event_cron flip_active_events emits a "start" transition next pass —
        # driving the WS broadcast + collective memory the direct is_active=True
        # path skipped (diagnosis §2.6). Off → keep the legacy immediate-active.
        we = WorldEvent(
            type="script",
            title=p.get("title", f"剧本 · 第{s.act}幕"),
            description=p.get("description", ""),
            starts_at=now,
            ends_at=now + timedelta(hours=int(p.get("duration_hours", 24))),
            is_active=(False if settings.realism_enabled else True),
            payload_json={"season_id": s.season_id, "act": s.act},
        )
        db.add(we)

        # 2. Public clue on the bulletin board.
        clue = p.get("clue")
        if clue:
            db.add(BulletinPost(
                kind="clue", author_user_id=None,
                title=p.get("clue_title", f"线索 · 第{s.act}幕"), content_md=clue,
            ))

        # 3. Private "secret" memories to named residents.
        injected = 0
        for sec in p.get("secrets", []) or []:
            slug = sec.get("resident_slug")
            content = sec.get("memory_content")
            if not slug or not content:
                continue
            from app.memory.service import MemoryService
            for res in await _resolve_secret_targets(db, slug):
                await MemoryService(db).add_memory(
                    res.id, "event", content, importance=float(sec.get("importance", 0.7)), source="script",
                )
                injected += 1

        s.status = "fired"
        await db.commit()
        try:
            from app.services.world_event_service import invalidate_active_cache
            invalidate_active_cache()
        except Exception:
            pass
        fired.append({"season_id": s.season_id, "act": s.act, "secrets_injected": injected})
    return fired


# --------------------------------------------------------------------------- #
# Polls                                                                        #
# --------------------------------------------------------------------------- #
async def open_polls(db, season_id: str | None = None, user_id: str | None = None) -> list[dict]:
    now = datetime.now(UTC)
    stmt = select(Poll).where(Poll.status == "open")
    if season_id is not None:
        stmt = stmt.where(Poll.season_id == season_id)
    polls = (await db.execute(stmt)).scalars().all()
    out = []
    for poll in polls:
        closes = poll.closes_at
        if closes is not None and closes.tzinfo is None:
            closes = closes.replace(tzinfo=UTC)
        if closes is not None and closes <= now:
            continue
        out.append({
            "id": poll.id, "season_id": poll.season_id, "question": poll.question,
            "options": poll.options_json or [], "closes_at": poll.closes_at.isoformat() if poll.closes_at else None,
        })
    # Let the UI restore the ✓已投 marker across reloads (my_vote = option idx).
    if user_id and out:
        votes = dict((await db.execute(
            select(Vote.poll_id, Vote.option_idx)
            .where(Vote.user_id == user_id, Vote.poll_id.in_([p["id"] for p in out]))
        )).all())
        for p in out:
            if p["id"] in votes:
                p["my_vote"] = votes[p["id"]]
    return out


async def cast_vote(db, poll_id: str, user_id: str, option_idx: int) -> None:
    # Read scalar columns rather than the ORM entity: a prior IntegrityError
    # rollback expires identity-map objects, and a later lazy attribute load
    # would raise MissingGreenlet outside an await.
    row = (await db.execute(
        select(Poll.status, Poll.closes_at, Poll.options_json).where(Poll.id == poll_id)
    )).first()
    if row is None or row.status != "open":
        raise PollError("poll is not open")
    closes = row.closes_at
    if closes is not None and closes.tzinfo is None:
        closes = closes.replace(tzinfo=UTC)
    if closes is not None and closes <= datetime.now(UTC):
        raise PollError("poll has closed")
    options = row.options_json or []
    if not (0 <= option_idx < len(options)):
        raise PollError("option_idx out of range")

    # Pre-check keeps the common double-vote case from doing an insert+rollback,
    # which would expire the caller's other in-session objects. The unique
    # constraint below is the real guard for the concurrent race.
    existing = (await db.execute(
        select(Vote.id).where(Vote.poll_id == poll_id, Vote.user_id == user_id)
    )).first()
    if existing is not None:
        raise PollError("already voted on this poll")

    db.add(Vote(poll_id=poll_id, user_id=user_id, option_idx=option_idx))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise PollError("already voted on this poll")


async def settle_season_polls(db, season_id: str) -> list[dict]:
    """Close every open poll for a season and record the winning option.

    Winner is the option with the most votes (ties → lowest index). Results are
    stashed on the poll's own row is not possible (no result column), so they are
    returned and also folded into the season payload by the caller."""
    polls = (await db.execute(
        select(Poll).where(Poll.season_id == season_id, Poll.status == "open")
    )).scalars().all()
    results = []
    for poll in polls:
        counts = dict((await db.execute(
            select(Vote.option_idx, func.count()).where(Vote.poll_id == poll.id).group_by(Vote.option_idx)
        )).all())
        options = poll.options_json or []
        winner_idx = None
        if counts:
            winner_idx = max(range(len(options)), key=lambda i: counts.get(i, 0))
        poll.status = "closed"
        results.append({
            "poll_id": poll.id, "question": poll.question,
            "winner_idx": winner_idx,
            "winner": options[winner_idx] if winner_idx is not None and winner_idx < len(options) else None,
            "counts": {str(k): v for k, v in counts.items()},
        })
    await db.commit()
    return results


# --------------------------------------------------------------------------- #
# Finale                                                                       #
# --------------------------------------------------------------------------- #
async def settle_due_seasons(db) -> list[dict]:
    """Settle every active season whose ends_at has passed: E12 scores + polls +
    a finale recap on the bulletin. Idempotent via Season.status/payload."""
    now = datetime.now(UTC)
    seasons = (await db.execute(select(Season).where(Season.status == "active"))).scalars().all()
    settled = []
    for season in seasons:
        ends = season.ends_at
        if ends is not None and ends.tzinfo is None:
            ends = ends.replace(tzinfo=UTC)
        if ends is None or ends > now:
            continue

        poll_results = await settle_season_polls(db, season.id)
        from app.services.season_service import settle_season
        payload = await settle_season(db, season)  # flips status→settled, scores top-3

        # Fold poll results into the settled payload for the record.
        merged = dict(season.payload_json or {})
        merged["poll_results"] = poll_results
        season.payload_json = merged
        await db.commit()

        # A5-style finale recap on the bulletin (template, no LLM).
        try:
            top = (payload.get("final_ranks") or [])[:3]
            lines = [f"# {season.title} · 落幕", "", f"主题：{season.theme or '—'}", ""]
            if top:
                lines.append("## 赛季榜前三")
                for e in top:
                    lines.append(f"{e['rank']}. `{e['user_id'][:8]}` — {e['points']} 分")
            if poll_results:
                lines.append("")
                lines.append("## 民意投票结果")
                for r in poll_results:
                    lines.append(f"- {r['question']} → **{r['winner'] or '（无人投票）'}**")
            db.add(BulletinPost(
                kind="digest", author_user_id=None, pinned=True,
                title=f"{season.title} · 赛季落幕", content_md="\n".join(lines),
            ))
            await db.commit()
        except Exception:
            logger.warning("season finale bulletin failed", exc_info=True)

        settled.append({"season_id": season.id, "polls_settled": len(poll_results)})
    return settled
