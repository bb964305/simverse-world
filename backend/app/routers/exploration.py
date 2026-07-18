"""E8 exploration codex endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.map_data import LOCATIONS
from app.agent.location_lore import HIDDEN_SPOTS, lore_for
from app.database import get_db
from app.models.location_visit import LocationVisit
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/exploration", tags=["exploration"])


@router.get("/me")
async def my_codex(request: Request, db: AsyncSession = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")

    visits = (await db.execute(
        select(LocationVisit).where(LocationVisit.user_id == user.id)
    )).scalars().all()
    by_loc = {v.location_id: v for v in visits}

    entries = []
    for loc_id, loc in LOCATIONS.items():
        if not loc.get("bounds"):
            continue
        v = by_loc.get(loc_id)
        entries.append({
            "location_id": loc_id,
            "name": loc.get("name", loc_id),
            "visited": v is not None,
            "visit_count": v.visit_count if v else 0,
            "secret_found": f"{loc_id}:secret" in by_loc,
            "has_secret": loc_id in HIDDEN_SPOTS,
            "lore": lore_for(loc_id) if v is not None else None,
            # Tile-space rect so the codex can draw the minimap silhouette.
            "bounds": list(loc["bounds"]),
        })
    visited = sum(1 for e in entries if e["visited"])
    return {"total": len(entries), "visited": visited, "locations": entries}
