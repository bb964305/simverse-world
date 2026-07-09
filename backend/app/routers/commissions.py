"""Commission board endpoints (B1)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.commission import Commission
from app.services.auth_service import get_current_user
from app.services.commission_service import accept, abandon, serialize, CommissionError

router = APIRouter(prefix="/commissions", tags=["commissions"])


async def _require_user(request: Request, db: AsyncSession):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return user


@router.get("")
async def list_commissions(request: Request, status: str = "open", db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    q = select(Commission).order_by(Commission.created_at.desc())
    if status == "mine":
        q = q.where(Commission.acceptor_user_id == user.id)
    else:
        q = q.where(Commission.status == status)
    rows = (await db.execute(q.limit(100))).scalars().all()
    return {"commissions": [serialize(c) for c in rows]}


@router.post("/{commission_id}/accept")
async def accept_commission(commission_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    try:
        c = await accept(db, commission_id, user.id)
    except CommissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return serialize(c)


@router.post("/{commission_id}/abandon")
async def abandon_commission(commission_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    try:
        await abandon(db, commission_id, user.id)
    except CommissionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}
