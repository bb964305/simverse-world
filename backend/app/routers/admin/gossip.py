"""Admin gossip / rumor-chain visualization (E3) — require_admin on all routes.

`gossip_service.get_rumor_chain` does the tracing; these routes add the admin
entry points: a recent-gossip listing (chain roots to click) and the chain
itself, enriched with resident names so the panel can render without N+1 calls.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.user import User
from app.routers.admin.middleware import require_admin
from app.services.gossip_service import get_rumor_chain

router = APIRouter(prefix="/gossip", tags=["admin-gossip"])


async def _resident_names(db: AsyncSession, resident_ids: set[str]) -> dict[str, dict]:
    if not resident_ids:
        return {}
    rows = (await db.execute(
        select(Resident.id, Resident.name, Resident.slug).where(Resident.id.in_(resident_ids))
    )).all()
    return {rid: {"name": name, "slug": slug} for rid, name, slug in rows}


@router.get("/recent")
async def recent_gossip(
    limit: int = Query(50, ge=1, le=200),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Most recent gossip memories (newest first) — the panel's entry list."""
    rows = (await db.execute(
        select(Memory)
        .where(Memory.source == "gossip")
        .order_by(Memory.created_at.desc())
        .limit(limit)
    )).scalars().all()
    names = await _resident_names(db, {m.resident_id for m in rows})
    items = []
    for m in rows:
        meta = m.metadata_json or {}
        info = names.get(m.resident_id, {})
        items.append({
            "id": m.id,
            "resident_id": m.resident_id,
            "resident_name": info.get("name"),
            "resident_slug": info.get("slug"),
            "content": m.content,
            "importance": m.importance,
            "hops": meta.get("hops", 0),
            "distorted": bool(meta.get("distorted", False)),
            "origin_memory_id": meta.get("origin_memory_id"),
            "created_at": m.created_at.isoformat() if m.created_at else None,
        })
    return {"items": items}


@router.get("/chains/{memory_id}")
async def rumor_chain(
    memory_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Full rumor chain for a memory. Clicking any hop resolves back to the
    origin (metadata origin_memory_id) so the whole chain is always shown."""
    mem = await db.get(Memory, memory_id)
    if mem is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    origin_id = (mem.metadata_json or {}).get("origin_memory_id") or mem.id

    chain = await get_rumor_chain(db, origin_id)

    # Enrich the service's bare dicts with names + importance/timestamps.
    mem_ids = [c["id"] for c in chain]
    detail_rows = (await db.execute(
        select(Memory.id, Memory.importance, Memory.created_at).where(Memory.id.in_(mem_ids))
    )).all() if mem_ids else []
    details = {mid: (imp, created) for mid, imp, created in detail_rows}
    names = await _resident_names(db, {c["resident_id"] for c in chain})

    enriched = []
    for c in chain:
        imp, created = details.get(c["id"], (None, None))
        info = names.get(c["resident_id"], {})
        enriched.append({
            **c,
            "hops": c.get("hops") or 0,
            "distorted": bool(c.get("distorted", False)),
            "resident_name": info.get("name"),
            "resident_slug": info.get("slug"),
            "importance": imp,
            "created_at": created.isoformat() if created else None,
        })
    enriched.sort(key=lambda x: (x["hops"], x["created_at"] or ""))
    return {"origin_memory_id": origin_id, "chain": enriched}
