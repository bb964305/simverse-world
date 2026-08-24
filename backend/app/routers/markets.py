"""Authenticated player API for the current caravan market session."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_current_user
from app.services import market_service

router = APIRouter(prefix="/markets", tags=["markets"])


async def _require_user(request: Request, db: AsyncSession):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return user


class MarketPurchaseBody(BaseModel):
    visit_id: str = Field(min_length=1, max_length=36)
    offer_code: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")


@router.get("/current")
async def get_current_market(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user(request, db)
    return await market_service.current_market(db, user_id=user.id)


@router.post("/current/purchases")
async def purchase_current_market(
    body: MarketPurchaseBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user(request, db)
    try:
        return await market_service.purchase(
            db,
            user_id=user.id,
            visit_id=body.visit_id,
            offer_code=body.offer_code,
            request_key=body.idempotency_key,
        )
    except market_service.MarketError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
