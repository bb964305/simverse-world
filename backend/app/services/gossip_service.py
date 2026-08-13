"""E3 gossip: information handoff between residents, with distortion over hops.

At a resident-resident chat wrap-up, the speaker may pass one third-party memory
to the listener. The further it travels (hops), the more likely it gets distorted
(LLM rewrite); otherwise it's relayed verbatim (saves a call). Chains are traced
via metadata_json.origin_memory_id.
"""

import random
import logging

from sqlalchemy import select

from app.config import settings
from app.llm.client import get_client
from app.llm.metering import record_usage
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.models.resident import Resident

logger = logging.getLogger(__name__)

GOSSIP_PROBABILITY = 0.3
MAX_HOPS = 4
IMPORTANCE_CAP = 0.7
EVENT_GOSSIP_MIN_IMPORTANCE = 0.3  # P2-6: low floor so event info survives a few ×0.8 hops

DISTORT_SYSTEM = "把下面这条传闻改写：夸大或改错一个细节，但保留主干。只输出改写后的一句话。"


def _extract_text(resp) -> str:
    for block in resp.content:
        if hasattr(block, "text"):
            return block.text
    return ""


async def _distort(content: str) -> str:
    client = get_client("system")
    model = settings.effective_model
    resp = await client.messages.create(
        model=model, max_tokens=120, system=DISTORT_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    await record_usage("gossip", model=model, owner="system", response=resp)
    return _extract_text(resp).strip() or content


async def maybe_gossip(db, speaker: Resident, listener: Resident, rng=random) -> Memory | None:
    """Maybe pass a third-party memory from speaker to listener. Returns the new memory."""
    # Duty system: an information-hub resident (酒馆老板) gossips more readily —
    # per-speaker multiplier on the base probability. The 0.9 cap only bounds
    # the multiplied surplus and never lowers the base, so tests (and ops) that
    # pin GOSSIP_PROBABILITY = 1.0 stay fully deterministic.
    from app.services.duty_service import perk as _duty_perk
    multiplier = _duty_perk(speaker, "gossip_multiplier", 1.0)
    probability = max(GOSSIP_PROBABILITY, min(0.9, GOSSIP_PROBABILITY * multiplier))
    if random.random() >= probability:
        return None

    # Candidate rumors the speaker could pass on. Fetch a superset (JSON event_id
    # can't be filtered in portable SQL) then classify in Python:
    #   classic  = a third-party memory about a real resident (importance ≥ 0.6);
    #   P2-6     = ANY memory carrying an event_id (first-hand world_event OR an
    #              already-relayed gossip), so a partially-known world event
    #              propagates "知情者→朋友→朋友的朋友" second-hand. Only when the
    #              info gradient is on. Gate off → the classic filter alone, i.e.
    #              behavior is byte-identical to pre-P2.
    fetched = list((await db.execute(
        select(Memory).where(
            Memory.resident_id == speaker.id,
            Memory.type == "event",
            Memory.importance >= EVENT_GOSSIP_MIN_IMPORTANCE,
        ).order_by(Memory.importance.desc()).limit(40)
    )).scalars().all())

    # A dedicated recent-event lane prevents 0.5/0.6 world-event memories from
    # being permanently hidden behind a resident's thousands of high-scoring
    # personal memories.  It is independently gated for production canaries.
    if (settings.realism_info_gradient_enabled
            and settings.realism_gossip_event_lane_enabled):
        event_rows = list((await db.execute(
            select(Memory).where(
                Memory.resident_id == speaker.id,
                Memory.type == "event",
                Memory.source.in_(("world_event", "gossip")),
                Memory.metadata_json["event_id"].as_string().is_not(None),
            ).order_by(Memory.created_at.desc()).limit(10)
        )).scalars().all())
        seen_ids = {m.id for m in fetched}
        fetched.extend(m for m in event_rows if m.id not in seen_ids)

        # Do not spend a gossip handoff telling the listener an event they
        # already know; diffusion probes dedupe residents, so duplicates are
        # pure write/LLM waste.
        known_rows = (await db.execute(
            select(Memory.metadata_json).where(
                Memory.resident_id == listener.id,
                Memory.type == "event",
                Memory.source.in_(("world_event", "gossip")),
                Memory.metadata_json["event_id"].as_string().is_not(None),
            )
        )).scalars().all()
        listener_event_ids = {
            meta.get("event_id") for meta in known_rows if isinstance(meta, dict)
        }
    else:
        listener_event_ids = set()

    def _is_candidate(m: Memory) -> bool:
        meta = m.metadata_json or {}
        if meta.get("hops", 0) >= MAX_HOPS:  # hops>=4 terminates the chain
            return False
        classic = (
            (m.importance or 0) >= 0.6
            and m.related_resident_id is not None
            and m.related_resident_id != listener.id
        )
        event_class = settings.realism_info_gradient_enabled and bool(meta.get("event_id"))
        if event_class and meta.get("event_id") in listener_event_ids:
            return False
        return classic or event_class

    usable = [m for m in fetched if _is_candidate(m)]
    if not usable:
        return None

    if settings.realism_relations_enabled:
        # P2-3: gossip flows along strong ties — weight each rumor by the speaker's
        # familiarity with its *subject* (floor + familiarity so a stranger's rumor
        # is still possible). Batched: one relations query, not one per candidate.
        from app.services import relation_service
        rmap = await relation_service.relations_for(db, speaker.id)
        floor = settings.realism_rel_gossip_fam_floor

        def _w(m):
            v = rmap.get(m.related_resident_id)
            return floor + (v.familiarity if v else 0.0)

        origin = relation_service.weighted_pick(usable, _w, rng)
    else:
        origin = usable[0]  # importance-ordered first (pre-P2 behavior)

    origin_hops = (origin.metadata_json or {}).get("hops", 0)
    new_hops = origin_hops + 1
    distorted = random.random() < min(0.2 * new_hops, 0.8)
    content = await _distort(origin.content) if distorted else origin.content
    importance = min((origin.importance or 0.0) * 0.8, IMPORTANCE_CAP)
    origin_meta = origin.metadata_json or {}
    origin_id = origin_meta.get("origin_memory_id") or origin.id

    new_meta = {"origin_memory_id": origin_id, "hops": new_hops, "distorted": distorted}
    # P2-6: second-hand memories inherit the source event_id so the diffusion
    # probe can follow "知情者→朋友→朋友的朋友" across hops (field must not drop).
    if origin_meta.get("event_id"):
        new_meta["event_id"] = origin_meta["event_id"]

    mem = await MemoryService(db).add_memory(
        listener.id, "event", content, importance=importance, source="gossip",
        related_resident_id=origin.related_resident_id,
        metadata_json=new_meta,
    )

    # Realism P1-11: being gossiped about (a distorted rumor, hops≥2, subject is a
    # real resident) is quietly unsettling — nudge the subject's mood.
    if settings.realism_enabled and new_hops >= 2 and origin.related_resident_id:
        try:
            from app.services.mood_service import apply_mood_event_by_id
            await apply_mood_event_by_id(
                db, origin.related_resident_id,
                settings.realism_gossip_victim_valence,
                settings.realism_gossip_victim_arousal)
        except Exception:
            logger.warning("gossip victim mood write-back failed", exc_info=True)

    return mem


async def get_rumor_chain(db, origin_memory_id: str) -> list[dict]:
    """Admin: all gossip memories tracing back to one origin (+ the origin)."""
    origin = await db.get(Memory, origin_memory_id)
    rows = (await db.execute(
        select(Memory).where(Memory.source == "gossip").order_by(Memory.created_at.asc())
    )).scalars().all()
    chain = []
    if origin is not None:
        chain.append({"id": origin.id, "resident_id": origin.resident_id, "content": origin.content,
                      "hops": 0, "distorted": False})
    for m in rows:
        meta = m.metadata_json or {}
        if meta.get("origin_memory_id") == origin_memory_id:
            chain.append({"id": m.id, "resident_id": m.resident_id, "content": m.content,
                          "hops": meta.get("hops"), "distorted": meta.get("distorted")})
    return chain
