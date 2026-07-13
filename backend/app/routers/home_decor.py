"""B3 home decor endpoints.

PUT /residents/{slug}/home/decor — owner-only full replace (403 non-owner /
400 validation); GET is public (visiting someone's home). Kept out of
routers/residents.py so that file stays under the size budget; extra path
segments mean no conflict with its /{slug} catch-alls.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers.residents import _require_user_auth
from app.services.home_decor_service import (
    DecorError,
    get_home_decor,
    set_home_decor,
)
from app.services.resident_service import get_resident_by_slug

router = APIRouter(prefix="/residents", tags=["home-decor"])


class DecorItemBody(BaseModel):
    item_code: str = Field(min_length=1, max_length=50)
    x: int
    y: int
    rot: int = 0


class DecorPutRequest(BaseModel):
    # Cap enforced in the service (DECOR_MAX_ITEMS) so it surfaces as a 400
    # with a readable message rather than a Pydantic 422.
    items: list[DecorItemBody] = Field(default_factory=list)


@router.get("/{slug}/home/decor")
async def get_decor(slug: str, db: AsyncSession = Depends(get_db)):
    resident = await get_resident_by_slug(db, slug)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    return get_home_decor(resident)


@router.put("/{slug}/home/decor")
async def put_decor(
    slug: str,
    body: DecorPutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user_auth(request, db)
    resident = await get_resident_by_slug(db, slug)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    if resident.resident_type != "player" or resident.creator_id != user.id:
        raise HTTPException(status_code=403, detail="只能装修自己的玩家居民住房")
    try:
        return await set_home_decor(db, resident, user.id, [i.model_dump() for i in body.items])
    except DecorError as e:
        raise HTTPException(status_code=400, detail=str(e))
