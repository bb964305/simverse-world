"""S1-1 public reputation axis.

Reputation is a slow, public signal derived without LLM calls. V1 stores the
projection in Resident.meta_json["reputation"], so the feature is
migration-free and can remain default-off until production calibration.

Tone is not a constant: each rumor is read through the relation affinity between
the memory's holder and its subject, so a well-liked resident accrues positive
evidence. ``rep_gossip_base_tone`` is only the bias for an unknown pair.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.resident_relation import ResidentRelation
from app.services.relation_service import canonical_pair


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


def vote_trust_delta(meta_json: dict | None) -> float:
    """声誉进入一张选票的**唯一**通道(F1 第 2 项)。

    候选集选取已与声誉解耦(``election_service.open_election``),名声只在
    ``civic_service._npc_choice`` 的打分里当一项权重。闸门关时返回 0.0,加到
    分数上是逐字节无影响。
    """
    if not settings.rep_enabled:
        return 0.0
    return settings.rep_vote_trust_weight * score_from_meta(meta_json)


#: 八卦语气对关系 affinity 的权重。本线不改 config.py（批次规则 §1-6），先落成
#: 模块常量，``getattr`` 间接读使收口时「加一行 rep_gossip_affinity_weight」成为
#: 纯配置改动、代码零 diff。取 3.0 的依据：在冻结的 base_tone=-0.3 下，符号翻转
#: 点落在 affinity=+0.1 —— 恰是一次送礼/投资（realism_rel_affinity_gift=0.1）或
#: 约 4 次正向闲聊（realism_rel_affinity_chat=0.03）的增量。
GOSSIP_AFFINITY_WEIGHT = 3.0


def _affinity_weight() -> float:
    return float(getattr(settings, "rep_gossip_affinity_weight", GOSSIP_AFFINITY_WEIGHT))


def gossip_tone(affinity: float | None, *, distorted: bool = False) -> float:
    """一条传闻的语气 = 传话人对当事人的态度。

    ``affinity`` 取 ``resident_relations`` 上「记忆持有者 × 被议论者」这一对的
    质量轴（``[-1, 1]``，规则驱动零 LLM）。二人无往来（无关系行）时退化为
    ``rep_gossip_base_tone`` —— 修复前那个恒定负值现在只是**偏置项**。
    """
    try:
        value = 0.0 if affinity is None else float(affinity)
    except (TypeError, ValueError):
        value = 0.0
    value = max(-1.0, min(1.0, value))
    tone = settings.rep_gossip_base_tone + _affinity_weight() * value
    if distorted:
        tone += settings.rep_distortion_penalty
    return max(settings.rep_min, min(settings.rep_max, tone))


def evidence_weight(importance: float | None, hops: int, tone: float) -> float:
    """单条传闻的贡献：重要性加权的语气，按传播跳数衰减。"""
    try:
        weight = max(0.0, float(importance or 0.0))
    except (TypeError, ValueError):
        weight = 0.0
    try:
        damping = 1.0 + max(0, int(hops))
    except (TypeError, ValueError):
        damping = 1.0
    return weight * tone / damping


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


@dataclass(frozen=True)
class ScoreRow:
    """一个居民的一次声誉投影结果（不含写入）。"""

    resident_id: str
    slug: str
    previous: float
    score: float
    samples: int


async def _scored_residents(db: AsyncSession) -> list[Resident]:
    """声誉是社会属性不是政治权利 → 人口口径 ``is_autonomous``（spec §4.4）。"""
    return list((await db.execute(
        select(Resident).where(Resident.is_autonomous)
    )).scalars().all())


async def _affinity_lookup(
    db: AsyncSession, pairs: set[tuple[str, str]]
) -> dict[tuple[str, str], float]:
    """一次批量读，把 canonical pair 映射到 affinity。

    ``ids`` 的规模上界是小镇人口（传话人与被议论者都是 residents 行），Postgres
    的绑定参数上限 65535 远在其上；测试用的 sqlite 只有几十行。
    """
    if not pairs:
        return {}
    ids = sorted({party for pair in pairs for party in pair})
    rows = (await db.execute(
        select(ResidentRelation).where(
            ResidentRelation.party_a.in_(ids),
            ResidentRelation.party_b.in_(ids),
        )
    )).scalars().all()
    return {(row.party_a, row.party_b): float(row.affinity or 0.0) for row in rows}


async def _score_all(db: AsyncSession, residents: list[Resident]) -> list[ScoreRow]:
    """三次批量读（居民已由调用方读入 / 记忆 / 关系），零 LLM，纯规则。"""
    ids = [resident.id for resident in residents]
    memories = (await db.execute(
        select(Memory).where(
            Memory.source == "gossip",
            Memory.related_resident_id.in_(ids),
            Memory.archived_at.is_(None),
        )
    )).scalars().all()

    pairs: set[tuple[str, str]] = set()
    for memory in memories:
        if memory.resident_id and memory.related_resident_id:
            party_a, _, party_b, _ = canonical_pair(
                memory.resident_id, memory.related_resident_id
            )
            pairs.add((party_a, party_b))
    affinity_by_pair = await _affinity_lookup(db, pairs)

    evidence: dict[str, list[float]] = {resident_id: [] for resident_id in ids}
    for memory in memories:
        metadata = memory.metadata_json or {}
        try:
            hops = max(0, int(metadata.get("hops", 0)))
        except (TypeError, ValueError):
            hops = 0
        affinity = 0.0
        if memory.resident_id:
            party_a, _, party_b, _ = canonical_pair(
                memory.resident_id, memory.related_resident_id
            )
            affinity = affinity_by_pair.get((party_a, party_b), 0.0)
        tone = gossip_tone(affinity, distorted=metadata.get("distorted") is True)
        evidence[memory.related_resident_id].append(
            evidence_weight(memory.importance, hops, tone)
        )

    alpha = max(0.0, min(1.0, settings.rep_ema_alpha))
    rows: list[ScoreRow] = []
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
        rows.append(ScoreRow(
            resident_id=resident.id,
            slug=resident.slug,
            previous=previous,
            score=_clamp((1.0 - alpha) * previous + alpha * raw),
            samples=len(samples),
        ))
    return rows


async def project(db: AsyncSession, *, force: bool = False) -> list[ScoreRow]:
    """只读投影:算出「今晚会写成什么」但一个字节都不落库。

    ``force=True`` 绕过 ``rep_enabled``,让开闸前的标定(``scripts/rep_calibrate.py``)
    能读到真实分布。与 ``recompute`` 共用 ``_score_all``,因此标定口径和夜间任务
    永远不会漂移。
    """
    if not (force or settings.rep_enabled):
        return []
    residents = await _scored_residents(db)
    if not residents:
        return []
    return await _score_all(db, residents)


async def recompute(db: AsyncSession) -> int:
    """Recompute every simulated resident's slow reputation projection."""
    if not settings.rep_enabled:
        return 0

    residents = await _scored_residents(db)
    if not residents:
        return 0

    rows = {row.resident_id: row for row in await _score_all(db, residents)}
    now = datetime.now(UTC).isoformat()
    for resident in residents:
        row = rows[resident.id]
        meta = dict(resident.meta_json or {})
        meta["reputation"] = {
            "score": row.score,
            "updated_at": now,
            "samples": row.samples,
        }
        resident.meta_json = meta
        flag_modified(resident, "meta_json")

    await db.commit()
    return len(residents)


# ── F1 第 3 项:用真实分布标定信用阈值 ──────────────────────────────────


class CalibrationError(RuntimeError):
    """样本无法给出一个可用的信用阈值(太少 / 退化)。"""


def _percentile(values_sorted: list[float], q: float) -> float:
    """最近秩分位数(无 numpy 依赖)。``q`` 取 [0, 1]。"""
    if not values_sorted:
        return 0.0
    n = len(values_sorted)
    index = math.ceil(max(0.0, min(1.0, q)) * n) - 1
    return values_sorted[max(0, min(n - 1, index))]


def describe(scores: Sequence[float]) -> dict[str, float]:
    """声誉分分布的形状——标定与探针共用的读数。"""
    values = sorted(float(score) for score in scores)
    if not values:
        return {
            "n": 0, "min": 0.0, "p10": 0.0, "p25": 0.0, "median": 0.0,
            "p75": 0.0, "p90": 0.0, "max": 0.0, "mean": 0.0,
            "negative_share": 0.0,
        }
    return {
        "n": len(values),
        "min": values[0],
        "p10": _percentile(values, 0.10),
        "p25": _percentile(values, 0.25),
        "median": _percentile(values, 0.50),
        "p75": _percentile(values, 0.75),
        "p90": _percentile(values, 0.90),
        "max": values[-1],
        "mean": sum(values) / len(values),
        "negative_share": sum(1 for v in values if v < 0) / len(values),
    }


def describe_affinity_coverage(affinities: Sequence[float]) -> dict[str, float]:
    """gossip 样本的『覆盖率』读数,和 ``describe()`` 关心的是两件不同的事。

    ``describe()`` 只看最终分数落在哪——负偏、正偏、居中。但
    ``gossip_tone(affinity)`` 在 ``affinity == 0``(两人无关系行,或
    canonical pair 查不到)时退化为 ``rep_gossip_base_tone``,也就是
    修复前那个恒定负值。如果生产里绝大多数 gossip 记忆的
    (holder, subject) pair 都查不到非零 affinity,population 的最终分数
    分布依然会是一条看起来正常的负偏曲线——和「机制生效但大家确实互相
    说坏话」长得一模一样。``describe()`` 分不清这两种情况,这个函数才能。

    调用方喂入的是 ``_score_all`` 内部循环里,查表得到的逐条 pair
    affinity(该值目前算完 tone 后即被丢弃);``covered`` 统计其中非零的
    条数——非零意味着这条 gossip 记忆真的读到了一条 ``resident_relations``
    行,而不是落在无关系的 fallback 上。
    """
    values = [float(affinity) for affinity in affinities]
    n = len(values)
    if n == 0:
        return {"n": 0, "covered": 0, "uncovered": 0, "coverage_share": 0.0}
    covered = sum(1 for value in values if value != 0.0)
    return {
        "n": n,
        "covered": covered,
        "uncovered": n - covered,
        "coverage_share": covered / n,
    }


def recommend_credit_min_score(
    scores: Sequence[float], reject_fraction: float = 0.15
) -> float:
    """给出使拒绝面**非空且非全量**的阈值,尽量贴近 ``reject_fraction``。

    只在相邻的两个**实际出现过的**分值之间取中点,因此返回值必然满足
    ``0 < |{s < T}| < n``——「拒绝面非空」是构造保证,不靠事后断言。
    """
    values = sorted(float(score) for score in scores)
    if len(values) < 2:
        raise CalibrationError(f"need at least 2 scores to calibrate, got {len(values)}")
    if not 0.0 < float(reject_fraction) < 1.0:
        raise CalibrationError(
            f"reject_fraction must be in (0, 1), got {reject_fraction!r}"
        )
    distinct = sorted(set(values))
    if len(distinct) < 2:
        raise CalibrationError(
            f"degenerate distribution: every score == {distinct[0]!r}"
        )
    target = float(reject_fraction) * len(values)
    best_gap: float | None = None
    best_threshold = 0.0
    for low, high in zip(distinct, distinct[1:]):
        threshold = (low + high) / 2.0
        rejected = sum(1 for value in values if value < threshold)
        gap = abs(rejected - target)
        if best_gap is None or gap < best_gap:
            best_gap, best_threshold = gap, threshold
    return best_threshold
