"""S1-1 public reputation axis.

Reputation is a slow, public signal derived without LLM calls. V1 stores the
projection in Resident.meta_json["reputation"], so the feature is
migration-free and can remain default-off until production calibration.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.memory import Memory
from app.models.resident import Resident


def _clamp(value: float) -> float:
    return max(settings.rep_min, min(settings.rep_max, value))


def score_from_meta(meta_json: dict | None) -> float:
    """Read a stored score defensively; malformed legacy JSON stays neutral."""
    raw = (meta_json or {}).get("reputation", {})
    try:
        return _clamp(float(raw.get("score", settings.rep_neutral)))
    except (AttributeError, TypeError, ValueError):
        return settings.rep_neutral


def credit_allowed(score: float) -> bool:
    """Pure credit-policy primitive for future IOU/credit callers."""
    return float(score) >= settings.rep_credit_min_score


async def get(db: AsyncSession, resident_id_or_slug: str) -> float:
    if not settings.rep_enabled:
        return settings.rep_neutral
    resident = (await db.execute(
        select(Resident).where(
            (Resident.id == resident_id_or_slug)
            | (Resident.slug == resident_id_or_slug)
        )
    )).scalar_one_or_none()
    return score_from_meta(resident.meta_json) if resident else settings.rep_neutral


async def get_many(
    db: AsyncSession,
    resident_ids: list[str],
) -> dict[str, float]:
    if not resident_ids:
        return {}
    if not settings.rep_enabled:
        return {resident_id: settings.rep_neutral for resident_id in resident_ids}
    rows = (await db.execute(
        select(Resident).where(Resident.id.in_(resident_ids))
    )).scalars().all()
    scores = {resident.id: score_from_meta(resident.meta_json) for resident in rows}
    return {
        resident_id: scores.get(resident_id, settings.rep_neutral)
        for resident_id in resident_ids
    }


async def recompute(db: AsyncSession) -> int:
    """Recompute every inhabitant's slow reputation projection in two batch reads.

    口径是**世界人口**（``Resident.is_autonomous``），不是选民集
    （``is_civic_voter``）——声誉是社会属性，不是政治权利。这行原本是裸的
    ``resident_type == "npc"``，是 ``civic_membership`` 收口时漏掉的第 11 处读；
    留着它的后果是被降级者退出夜间重算、分数永久冻结在降级前那一刻，而
    ``election_service.py:53-60`` 的候选排序读的正是这个冻结值；未来「违规扣
    声誉」若先改档位再扣分，扣分动作也会因这行字面量永远不生效。
    """
    if not settings.rep_enabled:
        return 0

    residents = (await db.execute(
        select(Resident).where(Resident.is_autonomous)
    )).scalars().all()
    if not residents:
        return 0

    ids = [resident.id for resident in residents]
    memories = (await db.execute(
        select(Memory).where(
            Memory.source == "gossip",
            Memory.related_resident_id.in_(ids),
            Memory.archived_at.is_(None),
        )
    )).scalars().all()

    evidence: dict[str, list[float]] = {resident_id: [] for resident_id in ids}
    for memory in memories:
        metadata = memory.metadata_json or {}
        try:
            hops = max(0, int(metadata.get("hops", 0)))
        except (TypeError, ValueError):
            hops = 0
        tone = settings.rep_gossip_base_tone
        if metadata.get("distorted") is True:
            tone += settings.rep_distortion_penalty
        importance = max(0.0, float(memory.importance or 0.0))
        evidence[memory.related_resident_id].append(
            importance * tone / (1.0 + hops)
        )

    now = datetime.now(UTC).isoformat()
    alpha = max(0.0, min(1.0, settings.rep_ema_alpha))
    for resident in residents:
        samples = evidence[resident.id]
        mood = resident.mood_json or {}
        try:
            mood_valence = float(mood.get("valence", 0.0))
        except (TypeError, ValueError):
            mood_valence = 0.0
        gossip_signal = sum(samples) / len(samples) if samples else 0.0
        raw = settings.rep_mood_weight * mood_valence + gossip_signal
        previous = score_from_meta(resident.meta_json)
        score = _clamp((1.0 - alpha) * previous + alpha * raw)

        meta = dict(resident.meta_json or {})
        meta["reputation"] = {
            "score": score,
            "updated_at": now,
            "samples": len(samples),
        }
        resident.meta_json = meta
        flag_modified(resident, "meta_json")

    await db.commit()
    return len(residents)
