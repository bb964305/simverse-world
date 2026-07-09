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


async def maybe_gossip(db, speaker: Resident, listener: Resident) -> Memory | None:
    """Maybe pass a third-party memory from speaker to listener. Returns the new memory."""
    if random.random() >= GOSSIP_PROBABILITY:
        return None

    candidates = (await db.execute(
        select(Memory).where(
            Memory.resident_id == speaker.id,
            Memory.type == "event",
            Memory.importance >= 0.6,
            Memory.related_resident_id.is_not(None),
            Memory.related_resident_id != listener.id,
        ).order_by(Memory.importance.desc()).limit(20)
    )).scalars().all()

    origin = None
    for m in candidates:
        if (m.metadata_json or {}).get("hops", 0) < MAX_HOPS:  # hops>=4 terminates
            origin = m
            break
    if origin is None:
        return None

    origin_hops = (origin.metadata_json or {}).get("hops", 0)
    new_hops = origin_hops + 1
    distorted = random.random() < min(0.2 * new_hops, 0.8)
    content = await _distort(origin.content) if distorted else origin.content
    importance = min((origin.importance or 0.0) * 0.8, IMPORTANCE_CAP)
    origin_id = (origin.metadata_json or {}).get("origin_memory_id") or origin.id

    return await MemoryService(db).add_memory(
        listener.id, "event", content, importance=importance, source="gossip",
        related_resident_id=origin.related_resident_id,
        metadata_json={"origin_memory_id": origin_id, "hops": new_hops, "distorted": distorted},
    )


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
