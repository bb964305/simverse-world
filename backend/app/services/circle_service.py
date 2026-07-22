"""P2 §7.2 — circle detection + the three consumers.

Nightly, connected components over *strong* resident-resident ties (familiarity
≥ threshold) partition the village into social circles. Pure Python union-find —
residents number in the low hundreds, so this is microseconds; no Louvain, no
new dependency. Each member's ``meta_json.circle_id`` is stamped and a JSON
snapshot is cached in Redis. Three consumers read the result:
  - the village digest gets one circle-activity line (existing digest call);
  - the admin ``GET /admin/social-graph`` endpoint serves nodes+edges+circles;
  - script secrets may target ``circle:<id>`` (expanded to the circle's members).

A circle's id is its smallest member id — stable across runs while membership
holds. Player↔resident ties are ignored here (circles are between residents).
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.resident import Resident
from app.models.resident_relation import ResidentRelation

logger = logging.getLogger(__name__)

SNAPSHOT_REDIS_KEY = "sv:social_circles"


async def compute_circles(db, threshold: float | None = None):
    """Return ``(components, strong_edges)``:
    - components: list of member-id sets, one per circle (size ≥ 2), each sorted
      deterministically by the caller when needed;
    - strong_edges: list of ``(a, b, familiarity)`` for the edges used.
    Only resident-resident ties with familiarity ≥ threshold are considered."""
    threshold = settings.realism_circle_threshold if threshold is None else threshold
    resident_ids = set((await db.execute(select(Resident.id))).scalars().all())
    rows = (await db.execute(
        select(ResidentRelation.party_a, ResidentRelation.party_b, ResidentRelation.familiarity)
        .where(
            ResidentRelation.familiarity >= threshold,
            ResidentRelation.party_a_type == "resident",
            ResidentRelation.party_b_type == "resident",
        )
    )).all()

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    strong_edges = []
    for a, b, fam in rows:
        if a in resident_ids and b in resident_ids:
            union(a, b)
            strong_edges.append((a, b, float(fam)))

    groups: dict[str, set[str]] = {}
    for node in list(parent.keys()):
        groups.setdefault(find(node), set()).add(node)
    components = [g for g in groups.values() if len(g) >= 2]
    return components, strong_edges


def _circle_id(component: set[str]) -> str:
    return min(component)


def _activity(component: set[str], edges) -> float:
    """Sum familiarity over the circle's internal edges — a proxy for how much
    the circle has been interacting (the digest's 'most active' pick)."""
    return sum(fam for a, b, fam in edges if a in component and b in component)


async def refresh_circles(db) -> dict:
    """Recompute circles, stamp ``meta_json.circle_id`` on every resident (and
    clear it for residents no longer in a circle), cache a JSON snapshot in
    Redis. Returns the snapshot. Idempotent."""
    components, edges = await compute_circles(db)
    id_to_circle: dict[str, str] = {}
    circles = []
    for comp in components:
        cid = _circle_id(comp)
        for m in comp:
            id_to_circle[m] = cid
        circles.append({
            "circle_id": cid,
            "members": sorted(comp),
            "size": len(comp),
            "activity": round(_activity(comp, edges), 4),
        })
    circles.sort(key=lambda c: (-c["activity"], c["circle_id"]))

    residents = (await db.execute(select(Resident))).scalars().all()
    for r in residents:
        meta = dict(r.meta_json or {})
        new_cid = id_to_circle.get(r.id)
        if meta.get("circle_id") != new_cid:
            if new_cid is None:
                meta.pop("circle_id", None)
            else:
                meta["circle_id"] = new_cid
            r.meta_json = meta
            flag_modified(r, "meta_json")
    await db.commit()

    snapshot = {"circles": circles, "count": len(circles)}
    try:  # best-effort cache for cross-process reads / observability
        from app.redis_client import get_redis
        await get_redis().set(SNAPSHOT_REDIS_KEY, json.dumps(snapshot))
    except Exception:
        logger.debug("circle snapshot cache failed", exc_info=True)
    return snapshot


async def expand_circle(db, circle_id: str) -> list[str]:
    """Resident ids whose stamped ``meta_json.circle_id`` == circle_id. Used by
    the script ``circle:<id>`` secret-targeting syntax. Empty until a refresh has
    stamped circles (i.e. while relations are off)."""
    residents = (await db.execute(select(Resident.id, Resident.meta_json))).all()
    return [rid for rid, meta in residents if (meta or {}).get("circle_id") == circle_id]


async def build_social_graph(db) -> dict:
    """Live nodes+edges+circles for the admin endpoint. Nodes carry the stamped
    circle_id; edges are all resident-resident relations; circles come from a
    fresh component pass (so it is correct even between nightly refreshes)."""
    residents = (await db.execute(
        select(Resident.id, Resident.name, Resident.slug, Resident.meta_json)
    )).all()
    nodes = [
        {"id": rid, "name": name, "slug": slug, "circle_id": (meta or {}).get("circle_id")}
        for rid, name, slug, meta in residents
    ]
    rel_rows = (await db.execute(
        select(
            ResidentRelation.party_a, ResidentRelation.party_b,
            ResidentRelation.familiarity, ResidentRelation.affinity,
            ResidentRelation.party_a_type, ResidentRelation.party_b_type,
        )
    )).all()
    edges = [
        {"a": a, "b": b, "familiarity": fam, "affinity": aff}
        for a, b, fam, aff, at, bt in rel_rows
        if at == "resident" and bt == "resident"
    ]
    components, strong_edges = await compute_circles(db)
    circles = [
        {"circle_id": _circle_id(c), "members": sorted(c), "size": len(c),
         "activity": round(_activity(c, strong_edges), 4)}
        for c in components
    ]
    circles.sort(key=lambda c: (-c["activity"], c["circle_id"]))
    return {"nodes": nodes, "edges": edges, "circles": circles}


async def digest_circle_line(db) -> str | None:
    """One line of circle dynamics for the village digest (zero new LLM — it just
    adds to the existing digest prompt material). Names the most-active circle.
    None when there are no circles yet."""
    components, edges = await compute_circles(db)
    if not components:
        return None
    best = max(components, key=lambda c: (_activity(c, edges), -len(c)))
    names = (await db.execute(
        select(Resident.name).where(Resident.id.in_(best)).order_by(Resident.name)
    )).scalars().all()
    if not names:
        return None
    shown = "、".join(names[:3]) + ("等" if len(names) > 3 else "")
    return f"{shown}这个圈子最近往来最密，本周对话最活跃。"
