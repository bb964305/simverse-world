"""Story-arc engine (M2) — advances kind='arc' goals by pure rules.

An arc goal (seeded by ``seed.preset_characters.seed_preset_arcs``) carries an
ordered ``milestones_json`` list; each milestone has a ``trigger`` that the
engine evaluates. Milestones are advanced **in order**: only the first not-yet
``done`` milestone is checked per pass, so an arc reads as a story, not a
checklist.

Run from ``nightly_cron`` (once a day, off the tick hot path). Zero LLM: every
trigger is a DB predicate. When the last milestone completes the arc resolves
(status='achieved') and fires the finale — a best-effort personality jump
(reusing the existing evolution key-event channel), a signed bulletin, a
one-time relation bump, and a feed event.

Trigger types
-------------
- ``relation``:   {"with": slug, "affinity_gte"?, "familiarity_gte"?}
- ``co_location``:{"with": slug, "location": loc_id, "times": N}
                  counts nights both parties stand inside the location bounds
                  (counter persisted on the milestone as ``_count``).
- ``count``:      {"metric": "feed:<kind>" | "memory" | "commission:<kind>",
                  "gte": N}

All fail-open: a broken trigger never blocks the rest of the sweep.
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC

from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.resident import Resident
from app.models.resident_goal import ResidentGoal

logger = logging.getLogger(__name__)


async def evaluate_arcs(db) -> int:
    """One nightly pass over all active arc goals. Returns milestones advanced."""
    if not settings.arc_engine_enabled:
        return 0
    goals = (await db.execute(
        select(ResidentGoal).where(
            ResidentGoal.kind == "arc", ResidentGoal.status == "active",
        )
    )).scalars().all()

    ids = {r.id: r for r in (await db.execute(select(Resident))).scalars().all()}
    slug_to_id = {r.slug: rid for rid, r in ids.items()}
    advanced = 0
    for goal in goals:
        try:
            advanced += await _advance_one(db, goal, ids, slug_to_id)
        except Exception:
            logger.warning("arc advance failed for goal %s", goal.id, exc_info=True)
    return advanced


async def _advance_one(db, goal, ids, slug_to_id) -> int:
    resident = ids.get(goal.resident_id)
    if resident is None:
        return 0
    milestones = list(goal.milestones_json or [])
    # first not-done milestone (sequential storytelling)
    idx = next((i for i, m in enumerate(milestones) if not m.get("done")), None)
    if idx is None:
        return 0
    milestone = milestones[idx]

    hit = await _check_trigger(db, resident, milestone, ids, slug_to_id)
    # co_location writes its running counter back even when not yet complete
    goal.milestones_json = milestones
    flag_modified(goal, "milestones_json")
    if not hit:
        await db.commit()
        return 0

    milestone["done"] = True
    milestone["at"] = datetime.now(UTC).isoformat()
    done_count = sum(1 for m in milestones if m.get("done"))
    goal.progress = min(1.0, done_count / max(1, len(milestones)))
    goal.updated_at = datetime.now(UTC)
    flag_modified(goal, "milestones_json")
    await db.commit()

    await _record_milestone(db, resident, goal, milestone, ids, slug_to_id)

    if done_count >= len(milestones):
        await _finale(db, resident, goal, ids, slug_to_id)
    return 1


# ── trigger evaluation ─────────────────────────────────────────────────

async def _check_trigger(db, resident, milestone, ids, slug_to_id) -> bool:
    trig = milestone.get("trigger") or {}
    ttype = trig.get("type")
    if ttype == "relation":
        return await _check_relation(db, resident, trig, slug_to_id)
    if ttype == "co_location":
        return await _check_co_location(db, resident, milestone, ids, slug_to_id)
    if ttype == "count":
        return await _check_count(db, resident, trig)
    return False


async def _check_relation(db, resident, trig, slug_to_id) -> bool:
    other_id = slug_to_id.get(trig.get("with"))
    if not other_id:
        return False
    from app.services import relation_service
    pair = await relation_service.get_pair(db, resident.id, other_id)
    if pair is None:
        return False
    if "affinity_gte" in trig and pair.affinity < trig["affinity_gte"]:
        return False
    if "familiarity_gte" in trig and pair.familiarity < trig["familiarity_gte"]:
        return False
    return True


async def _check_co_location(db, resident, milestone, ids, slug_to_id) -> bool:
    """Increment a per-milestone counter each night both parties currently
    stand inside the named location's bounds; complete at ``times``."""
    trig = milestone.get("trigger") or {}
    other = ids.get(slug_to_id.get(trig.get("with")))
    if other is None:
        return False
    from app.agent.map_data import get_location_by_id
    loc = get_location_by_id(trig.get("location"))
    if not loc or not loc.get("bounds"):
        return False
    x1, y1, x2, y2 = loc["bounds"]

    def _inside(r):
        return x1 <= (r.tile_x or -1) <= x2 and y1 <= (r.tile_y or -1) <= y2

    if _inside(resident) and _inside(other):
        milestone["_count"] = int(milestone.get("_count", 0)) + 1
    return int(milestone.get("_count", 0)) >= int(trig.get("times", 1))


async def _check_count(db, resident, trig) -> bool:
    metric = trig.get("metric", "")
    need = int(trig.get("gte", 1))
    total = 0
    if metric.startswith("feed:"):
        from app.models.feed import FeedEvent
        kind = metric.split(":", 1)[1]
        total = (await db.execute(
            select(func.count()).select_from(FeedEvent).where(
                FeedEvent.resident_slug == resident.slug, FeedEvent.kind == kind,
            )
        )).scalar() or 0
    elif metric == "memory":
        from app.models.memory import Memory
        total = (await db.execute(
            select(func.count()).select_from(Memory).where(Memory.resident_id == resident.id)
        )).scalar() or 0
    elif metric.startswith("commission:"):
        from app.models.commission import Commission
        kind = metric.split(":", 1)[1]
        total = (await db.execute(
            select(func.count()).select_from(Commission).where(
                Commission.issuer_resident_id == resident.id,
                Commission.kind == kind, Commission.status == "completed",
            )
        )).scalar() or 0
    return total >= need


# ── effects ────────────────────────────────────────────────────────────

async def _record_milestone(db, resident, goal, milestone, ids, slug_to_id) -> None:
    from app.memory.service import MemoryService
    svc = MemoryService(db)
    title = milestone.get("title", "")
    await svc.add_memory(
        resident.id, "event",
        f"人生进展:{title}。（{goal.title}）", 0.8, "reflection",
    )
    # co-star (if any) remembers it too
    trig = milestone.get("trigger") or {}
    other = ids.get(slug_to_id.get(trig.get("with")))
    if other is not None:
        await svc.add_memory(
            other.id, "event",
            f"和{resident.name}之间有了进展:{title}。", 0.7, "observation",
            related_resident_id=resident.id,
        )
    await _feed(resident.slug, "goal_milestone", {"goal": goal.title, "milestone": title})


async def _finale(db, resident, goal, ids, slug_to_id) -> None:
    goal.status = "achieved"
    goal.progress = 1.0
    goal.resolved_at = datetime.now(UTC)
    await db.commit()

    # 1) one-time relation bump toward the arc's co-star (reconciliation payoff)
    costar_id = None
    for m in goal.milestones_json or []:
        w = (m.get("trigger") or {}).get("with")
        if w:
            costar_id = slug_to_id.get(w)
    if costar_id:
        try:
            from app.services import relation_service
            await relation_service.bump(db, resident.id, costar_id,
                                        d_familiarity=0.05, d_affinity=0.15)
        except Exception:
            logger.warning("arc finale relation bump failed", exc_info=True)

    # 2) signed bulletin — the town reads about it
    try:
        from app.services.bulletin_service import create_post
        await create_post(
            db, "journal", f"{resident.name}:{goal.title}",
            f"经过这些日子,{resident.name}终于走到了这一步——{goal.title}。",
            author_resident_id=resident.id,
        )
    except Exception:
        logger.warning("arc finale bulletin failed", exc_info=True)

    # 3) best-effort personality jump via the existing key-event channel.
    # Only when an LLM is actually configured (the shift eval is an LLM call);
    # otherwise the finale is still complete without it.
    try:
        from app.memory.service import MemoryService
        mem = await MemoryService(db).add_memory(
            resident.id, "event",
            f"我终于{goal.title}。这件一直压在心里的事,今天有了结果。",
            0.95, "reflection",
        )
        if settings.effective_api_key:
            from app.personality.evolution import EvolutionService
            await EvolutionService(db).evaluate_shift(resident, mem)
    except Exception:
        logger.warning("arc finale personality shift failed", exc_info=True)

    await _feed(resident.slug, "goal_achieved", {"goal": goal.title})


async def _feed(slug: str, kind: str, payload: dict) -> None:
    try:
        from app.services.feed_service import push
        await push(slug, kind, payload)
    except Exception:
        logger.debug("arc feed push failed for %s", slug, exc_info=True)
