"""Civic governance (M3) — proposals → clerk bulletin → NPC+player vote →
execute the winning outcome.

Built entirely on existing pieces: the ``polls``/``votes`` tables (C3), the
town-clerk bulletin duty (M0), and the same three landing channels the Lab
uses (system_config / dynamic_locations overlay + reload / world events).

A civic poll's ``options_json`` entry is::

    {"label": "支持", "effect": {...} | None, "npc_votes": 0}

``effect`` on the winning option is dispatched by :func:`_execute_outcome`.
NPC votes are rule-based (SBTI leaning + relationship to proposer + duty
interest); player votes come through the existing ``votes`` table. Everything
is fail-open and gated by ``settings.civic_polls_enabled``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC

from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.resident import Resident
from app.models.season import Poll, Vote

logger = logging.getLogger(__name__)


async def propose(
    db,
    topic: str,
    options: list[dict],
    *,
    proposer_slug: str | None = None,
    days: int | None = None,
) -> Poll | None:
    """Open a civic poll and have the town clerk announce it.

    ``options`` is a list of ``{"label": str, "effect": dict | None}``. Returns
    the Poll, or None when civic polls are disabled.
    """
    if not settings.civic_polls_enabled:
        return None
    window = days if days is not None else settings.civic_poll_days
    opts = [
        {"label": o["label"], "effect": o.get("effect"), "npc_votes": 0}
        for o in options
    ]
    if proposer_slug:
        # Same blob-on-opts[0] convention as _npc_voters: the proposer travels
        # with the poll so NPC voting can weigh the relationship (option 0 is
        # the proposer's lead option by convention).
        opts[0]["_proposer_slug"] = proposer_slug
    poll = Poll(
        question=topic,
        options_json=opts,
        closes_at=datetime.now(UTC) + timedelta(days=window),
        status="open",
    )
    db.add(poll)
    await db.commit()
    await db.refresh(poll)

    proposer_line = ""
    if proposer_slug:
        proposer = (await db.execute(
            select(Resident).where(Resident.slug == proposer_slug)
        )).scalar_one_or_none()
        if proposer is not None:
            proposer_line = f"本案由 {proposer.name} 提议。"
    await _clerk_announce(
        db,
        f"镇务征询:{topic}",
        f"现就「{topic}」公开征询全镇意见,选项:{'、'.join(o['label'] for o in opts)}。"
        f"{proposer_line}投票于 {poll.closes_at.date()} 截止,请各位居民踊跃参与。",
    )
    return poll


# ── M5: governance-driven building agenda ──────────────────────────────

CIVIC_AGENDA: list[dict] = [
    {
        "topic": "在南苑空地兴建一座邮局",
        "proposer_slug": "jiang-lin",
        "options": [
            {"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
                "slug": "post_office", "name": "邮局", "type": "public", "role": "logistics",
                "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
                "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
                "boosted_actions": ["WORK"],
            }}},
            {"label": "暂缓,维持现状", "effect": None},
        ],
    },
    {
        "topic": "在东岸花园兴建一座剧院",
        "proposer_slug": "zhou-dahe",
        "options": [
            {"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
                "slug": "theater", "name": "剧院", "type": "public", "role": "culture",
                "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
                "description": "小镇剧院:说书、演展、故事会的舞台",
                "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
            }}},
            {"label": "暂缓,维持现状", "effect": None},
        ],
    },
]


async def seed_civic_agenda(db) -> int:
    """Open the standing building proposals (idempotent — a topic already having
    a poll is skipped). These走 the full propose→vote→close→build流程, so the
    town's expansion is itself a civic event. Returns polls opened."""
    if not settings.civic_polls_enabled:
        return 0
    opened = 0
    for item in CIVIC_AGENDA:
        exists = (await db.execute(
            select(Poll).where(Poll.question == item["topic"])
        )).scalars().first()
        if exists:
            continue
        poll = await propose(
            db, item["topic"], item["options"],
            proposer_slug=item.get("proposer_slug"),
        )
        if poll is not None:
            opened += 1
    return opened


# ── NPC voting (rule-based, zero LLM) ──────────────────────────────────

async def run_npc_voting(db) -> int:
    """Each NPC casts one rule-based vote on each open poll it hasn't voted on.

    Votes accumulate in ``options_json[i]['npc_votes']``; a per-poll set of
    voter slugs (stored on the poll options blob under ``_npc_voters``) keeps
    it idempotent across nightly runs. Returns the number of votes cast.
    """
    if not settings.civic_polls_enabled:
        return 0
    polls = (await db.execute(select(Poll).where(Poll.status == "open"))).scalars().all()
    if not polls:
        return 0
    residents = (await db.execute(
        select(Resident).where(Resident.resident_type == "npc")
    )).scalars().all()
    if not residents:
        return 0

    from app.services import relation_service
    by_slug = {r.slug: r for r in residents}
    cast = 0
    for poll in polls:
        opts = list(poll.options_json or [])
        if not opts:
            continue
        voters = set(poll.options_json[0].get("_npc_voters", [])) if opts else set()
        for r in residents:
            if r.slug in voters:
                continue
            idx = await _npc_choice(db, r, poll, opts, relation_service, by_slug)
            opts[idx]["npc_votes"] = int(opts[idx].get("npc_votes", 0)) + 1
            voters.add(r.slug)
            cast += 1
        opts[0]["_npc_voters"] = sorted(voters)
        poll.options_json = opts
        flag_modified(poll, "options_json")
    if cast:
        await db.commit()
    return cast


async def _npc_choice(db, resident, poll, opts, relation_service, by_slug=None) -> int:
    """Score each option for this resident and return the best index.

    Heuristics (all rule-based):
      - conservative residents (SBTI A2=H, 守序) lean to the status-quo /
        no-effect option;
      - a positive tie to the proposer nudges toward the proposer's lead option
        (index 0 by convention); the proposer backs their own proposal;
      - a shopkeeper/economy duty leans toward options whose effect touches the
        shop or economy.
    """
    sbti = (resident.meta_json or {}).get("sbti", {})
    dims = sbti.get("dimensions", {})
    a2 = dims.get("A2", "M")  # 规则与灵活度: H=守序
    duty = (resident.meta_json or {}).get("duty", {}).get("key")

    scores = [0.0] * len(opts)
    for i, o in enumerate(opts):
        eff = o.get("effect")
        # conservative → prefer the no-effect / status-quo option
        if a2 == "H" and not eff:
            scores[i] += 1.0
        if a2 == "L" and eff:
            scores[i] += 0.5  # 叛逆者 lean toward change
        # duty interest
        if eff and duty in ("shop_keeper", "tavern_hub", "cafe_host"):
            blob = str(eff)
            if any(k in blob for k in ("shop", "market", "经济", "price")):
                scores[i] += 0.8

    # Relationship-to-proposer nudge toward option 0 (the proposer's lead).
    proposer_slug = opts[0].get("_proposer_slug") if opts else None
    if proposer_slug:
        if resident.slug == proposer_slug:
            scores[0] += 2.0  # you back your own proposal
        else:
            proposer = (by_slug or {}).get(proposer_slug)
            if proposer is not None:
                try:
                    pair = await relation_service.get_pair(db, resident.id, proposer.id)
                    if pair is not None and pair.affinity > 0:
                        # a close friend's ask outweighs mild conservatism
                        scores[0] += 1.5 * pair.affinity
                except Exception:
                    logger.debug("proposer relation lookup failed", exc_info=True)
    # deterministic tie-break: index order
    best = max(range(len(opts)), key=lambda i: (scores[i], -i))
    return best


# ── closing + execution ────────────────────────────────────────────────

async def close_due_polls(db, now: datetime | None = None) -> int:
    """Close every open poll past its ``closes_at``, tally NPC+player votes,
    execute the winner's effect, and announce the result. Returns count closed."""
    if not settings.civic_polls_enabled:
        return 0
    now = now or datetime.now(UTC)
    polls = (await db.execute(select(Poll).where(Poll.status == "open"))).scalars().all()
    closed = 0
    for poll in polls:
        due = poll.closes_at
        if due is not None and due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        if due is not None and due > now:
            continue
        try:
            await _close_one(db, poll)
            closed += 1
        except Exception:
            logger.warning("closing civic poll %s failed", poll.id, exc_info=True)
    return closed


async def _close_one(db, poll: Poll) -> None:
    opts = list(poll.options_json or [])
    # player votes from the votes table
    rows = (await db.execute(
        select(Vote.option_idx, func.count()).where(Vote.poll_id == poll.id).group_by(Vote.option_idx)
    )).all()
    player_votes = {idx: n for idx, n in rows}
    tally = [
        int(o.get("npc_votes", 0)) + int(player_votes.get(i, 0))
        for i, o in enumerate(opts)
    ]
    poll.status = "closed"
    if not tally:
        await db.commit()
        return
    win = max(range(len(tally)), key=lambda i: (tally[i], -i))
    opts[win]["won"] = True
    opts[win]["final_votes"] = tally[win]
    poll.options_json = opts
    flag_modified(poll, "options_json")
    await db.commit()

    effect = opts[win].get("effect")
    result_note = f"「{poll.question}」投票结束,「{opts[win]['label']}」以 {tally[win]} 票胜出。"
    if effect:
        applied = await _execute_outcome(db, effect)
        result_note += "议案已生效。" if applied else "议案生效时遇到问题,已记录。"
    await _clerk_announce(db, f"镇务结果:{poll.question}", result_note)


async def _execute_outcome(db, effect: dict) -> bool:
    """Land a winning outcome through an existing channel. Returns success."""
    etype = effect.get("type")
    try:
        if etype == "system_config":
            from app.services.config_service import ConfigService
            await ConfigService(db).set(
                effect["key"], effect["value"],
                group=effect.get("group", "civic"), updated_by="civic_vote",
            )
            return True
        if etype == "dynamic_location":
            return await _add_dynamic_location(db, effect["data"])
        if etype == "narrative":
            from app.models.world_event import WorldEvent
            ev = effect.get("event", {})
            now = datetime.now(UTC)
            db.add(WorldEvent(
                type="news", title=ev.get("title", "镇务公告"),
                description=ev.get("description", ""),
                payload_json=ev.get("payload", {}),
                starts_at=now, ends_at=now + timedelta(days=ev.get("days", 1)),
                is_active=False,
            ))
            await db.commit()
            return True
        if etype == "mayor":
            from app.services.election_service import install_mayor
            return await install_mayor(db, effect.get("slug"))
    except Exception:
        logger.warning("civic outcome execution failed (%s)", etype, exc_info=True)
    return False


async def _add_dynamic_location(db, data: dict) -> bool:
    """Insert a dynamic_locations overlay row + trigger the world reload so the
    new building is reachable without a redeploy."""
    from app.models.dynamic_location import DynamicLocation
    slug = data.get("slug")
    if not slug or "bounds" not in data:
        return False
    existing = (await db.execute(
        select(DynamicLocation).where(DynamicLocation.slug == slug)
    )).scalar_one_or_none()
    payload = {k: v for k, v in data.items() if k != "slug"}
    if existing is None:
        db.add(DynamicLocation(slug=slug, data_json=payload, active=True))
    else:
        existing.data_json = payload
        existing.active = True
    await db.commit()
    try:
        from app.lab.apply import reload_world, publish_world_reload
        await reload_world()
        await publish_world_reload()
    except Exception:
        logger.warning("world reload after civic build failed", exc_info=True)
    return True


# ── F3.3: public lecture → resident debate ─────────────────────────────

async def maybe_spawn_lecture_debate(db, event: dict) -> bool:
    """When a lecturer's public-lecture event ends, spin up a resident debate on
    the lecture topic between two socially-active, SBTI-contrasting residents.
    Best-effort; returns True if a debate was created."""
    if not settings.civic_polls_enabled:
        return False
    payload = event.get("payload_json") or {}
    if payload.get("duty") != "lecturer":
        return False
    try:
        residents = (await db.execute(
            select(Resident).where(Resident.resident_type == "npc")
        )).scalars().all()
        # socially active (So1 != L), lecturer excluded
        pool = []
        for r in residents:
            dims = (r.meta_json or {}).get("sbti", {}).get("dimensions", {})
            if dims.get("So1") == "L":
                continue
            if (r.meta_json or {}).get("duty", {}).get("key") == "lecturer":
                continue
            pool.append(r)
        if len(pool) < 2:
            return False
        # contrast on A1 (worldview): pick one optimistic, one skeptical if we can
        hi = next((r for r in pool if (r.meta_json or {}).get("sbti", {})
                   .get("dimensions", {}).get("A1") == "H"), None)
        lo = next((r for r in pool if (r.meta_json or {}).get("sbti", {})
                   .get("dimensions", {}).get("A1") == "L"), None)
        a = hi or pool[0]
        b = lo or next((r for r in pool if r.id != a.id), None)
        if b is None or a.id == b.id:
            return False
        topic = event.get("title", "小镇议题").replace("的公开课", "")
        from app.services.debate_service import create_debate
        await create_debate(db, f"关于「{topic}」的争论", a.slug, b.slug)
        return True
    except Exception:
        logger.warning("lecture debate spawn failed", exc_info=True)
        return False


# ── helper ─────────────────────────────────────────────────────────────

async def _clerk_announce(db, title: str, body: str) -> None:
    try:
        from app.services.bulletin_service import create_post
        from app.services.duty_service import find_duty_resident
        clerk = await find_duty_resident(db, "town_clerk")
        author_id = clerk.id if clerk else None
        await create_post(db, "notice", title, body, author_resident_id=author_id)
    except Exception:
        logger.warning("civic clerk announce failed", exc_info=True)
