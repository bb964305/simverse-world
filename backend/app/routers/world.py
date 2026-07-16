"""Public world data (P3): the static + dynamic-overlay merged location snapshot
that minimap/codex render from, so an approved dynamic building shows up without
a frontend redeploy (spec §7, §9)."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(prefix="/world", tags=["world"])


@router.get("/locations")
async def get_locations(db: AsyncSession = Depends(get_db)):
    from app.agent import map_data

    # Idempotent re-merge so a caller always gets the freshest overlay even if a
    # reload signal was missed (e.g. this worker just started).
    await map_data.load_dynamic_locations()
    out = []
    for slug, loc in map_data.LOCATIONS.items():
        bounds = loc.get("bounds")
        center = loc.get("center")
        entrance = loc.get("entrance")
        out.append({
            "slug": slug,
            "name": loc.get("name"),
            "type": loc.get("type"),
            "role": loc.get("role"),
            "bounds": list(bounds) if bounds else None,
            "center": list(center) if center else None,
            "entrance": list(entrance) if entrance else None,
            "description": loc.get("description"),
            "boosted_actions": loc.get("boosted_actions", []),
            "dynamic": slug in map_data._dynamic_slugs,
        })
    return {"locations": out}
