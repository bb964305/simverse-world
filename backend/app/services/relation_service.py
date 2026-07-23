"""Numeric two-axis relationship service (P2 §7.1).

All writes are **atomic** — a single conditional ``UPDATE`` (``SET col = clamp(col
+ :d)``) plus an insert-on-miss upsert, never read-modify-write — so concurrent
``bump`` calls from multiple tick workers cannot lose updates (same standard as
``coin_service`` P0-5). Pairs are stored under a canonical undirected key so a
tie is one row regardless of argument order.

The clamp is expressed as a portable ``CASE`` rather than Postgres ``LEAST`` /
``GREATEST`` (which SQLite lacks) so the same statement runs on the test SQLite
and production Postgres — see PROGRESS (P2-1 deviation).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

from sqlalchemy import select, update, case, and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.resident_relation import ResidentRelation


def weighted_pick(items, weight_fn, rng, epsilon: float = 0.0):
    """Pick one item weighted by ``weight_fn(item) >= 0``, deterministic under a
    seeded ``rng``. With probability ``epsilon`` a *uniform* pick is made instead
    (the ε mixing mass that keeps low-weight items — strangers — reachable so
    circles don't ossify). Falls back to uniform when all weights are 0."""
    if not items:
        return None
    if epsilon and rng.random() < epsilon:
        return rng.choice(items)
    weights = [max(0.0, float(weight_fn(it))) for it in items]
    if sum(weights) <= 0:
        return rng.choice(items)
    return rng.choices(items, weights=weights, k=1)[0]


def turns_for_familiarity(familiarity: float, lo: int = 3, hi: int = 8) -> int:
    """Map familiarity [0,1] linearly onto the conversation-length band [lo, hi]:
    strangers talk briefly (≈3-4 turns), old friends linger (≈6-8). Deterministic
    (fully reproducible) — old-friend length is a property of the tie, not a dice
    roll."""
    n = int(round(lo + (hi - lo) * max(0.0, min(1.0, familiarity))))
    return max(lo, min(hi, n))


def canonical_pair(
    id1: str, id2: str, type1: str = "resident", type2: str = "resident"
) -> tuple[str, str, str, str]:
    """Return ``(party_a, party_a_type, party_b, party_b_type)`` with the
    smaller id first (canonical undirected key). The type travels with its id."""
    if id1 <= id2:
        return id1, type1, id2, type2
    return id2, type2, id1, type1


def _clamp(col, delta: float, lo: float, hi: float):
    """Portable ``clamp(col + delta, lo, hi)`` as a CASE expression."""
    expr = col + delta
    return case((expr > hi, hi), (expr < lo, lo), else_=expr)


@dataclass(frozen=True)
class RelationView:
    """A relation as seen from one party's side (the *other* party exposed)."""

    other_id: str
    other_type: str
    familiarity: float
    affinity: float
    interact_count: int
    last_interact_at: datetime | None


async def bump(
    db: AsyncSession,
    id1: str,
    id2: str,
    d_familiarity: float = 0.0,
    d_affinity: float = 0.0,
    *,
    type1: str = "resident",
    type2: str = "resident",
    now: datetime | None = None,
) -> None:
    """Atomically apply deltas to a canonical pair (upsert).

    familiarity is clamped to [0, 1]; affinity to [-1, 1]. Self-pairs are a
    no-op. A concurrent insert (IntegrityError) falls back to the UPDATE path,
    so no bump is ever lost.
    """
    if id1 == id2:
        return
    now = now or datetime.now(UTC)
    pa, pat, pb, pbt = canonical_pair(id1, id2, type1, type2)

    async def _do_update() -> int:
        res = await db.execute(
            update(ResidentRelation)
            .where(ResidentRelation.party_a == pa, ResidentRelation.party_b == pb)
            .values(
                familiarity=_clamp(ResidentRelation.familiarity, d_familiarity, 0.0, 1.0),
                affinity=_clamp(ResidentRelation.affinity, d_affinity, -1.0, 1.0),
                interact_count=ResidentRelation.interact_count + 1,
                last_interact_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return res.rowcount or 0

    if await _do_update() > 0:
        await db.commit()
        return

    # Miss → insert the row with clamped initial values.
    row = ResidentRelation(
        party_a=pa, party_a_type=pat, party_b=pb, party_b_type=pbt,
        familiarity=min(1.0, max(0.0, d_familiarity)),
        affinity=min(1.0, max(-1.0, d_affinity)),
        interact_count=1,
        last_interact_at=now,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # Race: another worker inserted the pair first → apply as an update.
        await db.rollback()
        if await _do_update() > 0:
            await db.commit()


async def get_pair(
    db: AsyncSession, id1: str, id2: str, type1: str = "resident", type2: str = "resident"
) -> ResidentRelation | None:
    pa, _, pb, _ = canonical_pair(id1, id2, type1, type2)
    res = await db.execute(
        select(ResidentRelation)
        .where(ResidentRelation.party_a == pa, ResidentRelation.party_b == pb)
        # Fresh read even if an earlier atomic bump in THIS session left the
        # identity-mapped row stale (synchronize_session=False, cf. coin_service).
        .execution_options(populate_existing=True)
    )
    return res.scalar_one_or_none()


async def top_relations(
    db: AsyncSession,
    party_id: str,
    n: int = 5,
    by: str = "familiarity",
    party_type: str = "resident",
) -> list[RelationView]:
    """Top-``n`` relations for one party, ordered by ``familiarity`` or
    ``affinity`` (descending), returned from that party's viewpoint."""
    col = ResidentRelation.affinity if by == "affinity" else ResidentRelation.familiarity
    res = await db.execute(
        select(ResidentRelation)
        .where(or_(ResidentRelation.party_a == party_id, ResidentRelation.party_b == party_id))
        .order_by(col.desc())
        .limit(n)
        .execution_options(populate_existing=True)
    )
    return [_view_from(r, party_id) for r in res.scalars().all()]


async def relations_for(
    db: AsyncSession, party_id: str, party_type: str = "resident"
) -> dict[str, RelationView]:
    """Batch-fetch every relation involving ``party_id`` as ``{other_id: view}``.

    One query — the read-path callers load this once into TickContext and do
    O(1) lookups, keeping the per-resident tick query count at +1 (perf red
    line: no per-candidate relation query).
    """
    res = await db.execute(
        select(ResidentRelation)
        .where(or_(ResidentRelation.party_a == party_id, ResidentRelation.party_b == party_id))
        .execution_options(populate_existing=True)
    )
    out: dict[str, RelationView] = {}
    for r in res.scalars().all():
        v = _view_from(r, party_id)
        out[v.other_id] = v
    return out


def _view_from(r: ResidentRelation, party_id: str) -> RelationView:
    if r.party_a == party_id:
        other_id, other_type = r.party_b, r.party_b_type
    else:
        other_id, other_type = r.party_a, r.party_a_type
    return RelationView(
        other_id=other_id,
        other_type=other_type,
        familiarity=r.familiarity,
        affinity=r.affinity,
        interact_count=r.interact_count,
        last_interact_at=r.last_interact_at,
    )


async def decay(db: AsyncSession, now: datetime | None = None, weeks: float = 1.0) -> int:
    """One weekly decay step over relations idle for ``realism_rel_decay_idle_days``:
    familiarity ×0.95, affinity ×0.98 (2% regression toward 0). One atomic
    UPDATE. Returns the number of rows affected. Estrangement is real."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=settings.realism_rel_decay_idle_days)
    fam_factor = settings.realism_rel_familiarity_decay ** weeks
    aff_factor = settings.realism_rel_affinity_decay ** weeks
    res = await db.execute(
        update(ResidentRelation)
        .where(ResidentRelation.last_interact_at < cutoff)
        .values(
            familiarity=ResidentRelation.familiarity * fam_factor,
            affinity=ResidentRelation.affinity * aff_factor,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return res.rowcount or 0
