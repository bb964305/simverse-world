"""C2 relationship graph aggregation (no new table).

Aggregates type='relationship' memories into a node/edge graph. Portable
Python-side grouping (no PG-specific array_agg) so it runs on sqlite too.
Cached process-locally for 10 minutes — the graph doesn't need to be realtime.
Player edges are returned only for the requesting user (privacy).
"""

import time
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.memory import Memory
from app.models.resident import Resident
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/graph", tags=["graph"])
logger = logging.getLogger(__name__)

_CACHE_TTL = 600.0
_cache: dict = {}  # min_importance -> (ts, {nodes, resident_edges})


def _invalidate():
    _cache.clear()


async def _resident_graph(db: AsyncSession, min_importance: float) -> dict:
    now = time.monotonic()
    hit = _cache.get(min_importance)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    residents = (await db.execute(select(Resident))).scalars().all()
    node_ids = {r.id for r in residents}
    nodes = [{"slug": r.slug, "name": r.name, "portrait_url": r.portrait_url,
              "district": r.district, "id": r.id} for r in residents]

    rels = (await db.execute(
        select(Memory).where(
            Memory.type == "relationship", Memory.related_resident_id.isnot(None),
        ).order_by(Memory.created_at.desc())
    )).scalars().all()

    # (a,b) -> {strength, label}; keep the latest content as label (rows are desc).
    pair: dict[tuple[str, str], dict] = {}
    for m in rels:
        if m.resident_id not in node_ids or m.related_resident_id not in node_ids:
            continue
        key = (m.resident_id, m.related_resident_id)
        entry = pair.setdefault(key, {"strength": 0.0, "label": m.content})
        entry["strength"] = max(entry["strength"], m.importance or 0.0)

    edges = []
    seen = set()
    for (a, b), info in pair.items():
        canonical = tuple(sorted((a, b)))
        if canonical in seen:
            continue
        reverse = pair.get((b, a))
        mutual = reverse is not None
        strength = max(info["strength"], reverse["strength"]) if mutual else info["strength"]
        if strength < min_importance:
            continue
        seen.add(canonical)
        edges.append({"a": a, "b": b, "strength": round(strength, 3),
                      "label": info["label"], "mutual": mutual})

    result = {"nodes": nodes, "edges": edges}
    _cache[min_importance] = (now, result)
    return result


@router.get("/relationships")
async def relationships(request: Request, min_importance: float = 0.3, db: AsyncSession = Depends(get_db)):
    base = await _resident_graph(db, min_importance)
    nodes = [{k: n[k] for k in ("slug", "name", "portrait_url", "district")} for n in base["nodes"]]
    id_to_slug = {n["id"]: n["slug"] for n in base["nodes"]}
    edges = [{"a": id_to_slug[e["a"]], "b": id_to_slug[e["b"]],
              "strength": e["strength"], "label": e["label"], "mutual": e["mutual"]}
             for e in base["edges"] if e["a"] in id_to_slug and e["b"] in id_to_slug]

    # Player's own edges only (privacy: never expose other players' relationships).
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        user = await get_current_user(db, auth.removeprefix("Bearer "))
        if user:
            you = {"slug": "__you__", "name": "你", "portrait_url": None, "district": ""}
            player_rels = (await db.execute(
                select(Memory).where(
                    Memory.type == "relationship", Memory.related_user_id == user.id,
                ).order_by(Memory.created_at.desc())
            )).scalars().all()
            by_res: dict[str, dict] = {}
            for m in player_rels:
                e = by_res.setdefault(m.resident_id, {"strength": 0.0, "label": m.content})
                e["strength"] = max(e["strength"], m.importance or 0.0)
            res_by_id = {n["id"]: n["slug"] for n in base["nodes"]}
            added = False
            for rid, info in by_res.items():
                if info["strength"] >= min_importance and rid in res_by_id:
                    edges.append({"a": "__you__", "b": res_by_id[rid],
                                  "strength": round(info["strength"], 3),
                                  "label": info["label"], "mutual": False})
                    added = True
            if added:
                nodes.append(you)

    return {"nodes": nodes, "edges": edges}
