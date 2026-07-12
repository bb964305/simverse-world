"""Time capsule endpoints (E7)."""

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.time_capsule import TimeCapsule
from app.services.auth_service import get_current_user
from app.services.capsule_service import create_capsule, serialize, CapsuleError

router = APIRouter(prefix="/capsules", tags=["capsules"])


async def _require_user(request: Request, db: AsyncSession):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return user


class CapsuleCreate(BaseModel):
    carrier_resident_slug: str
    deliver_on: date_type
    content: str


@router.post("")
async def post_capsule(body: CapsuleCreate, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    try:
        c = await create_capsule(db, user.id, body.carrier_resident_slug, body.deliver_on, body.content)
    except CapsuleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return serialize(c, include_content=True)


@router.get("")
async def my_capsules(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    rows = (await db.execute(
        select(TimeCapsule).where(TimeCapsule.user_id == user.id).order_by(TimeCapsule.deliver_on)
        # P1-3 audit: per-user but unbounded over time; cap keeps the payload sane.
        .limit(200)
    )).scalars().all()
    return {"capsules": [serialize(c, include_content=True) for c in rows]}


@router.get("/{capsule_id}")
async def get_capsule(capsule_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    c = await db.get(TimeCapsule, capsule_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Privacy: sealed content is only visible to the owner.
    is_owner = c.user_id == user.id
    if not is_owner and c.status == "sealed":
        raise HTTPException(status_code=403, detail="Sealed capsule")
    return serialize(c, include_content=is_owner)
