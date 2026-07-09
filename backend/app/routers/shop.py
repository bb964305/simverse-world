"""Shop endpoints (S3): catalog + purchase."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_current_user
from app.services.shop_service import get_catalog, purchase, ShopError

router = APIRouter(prefix="/shop", tags=["shop"])


async def _require_user(request: Request, db: AsyncSession):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return user


class PurchaseRequest(BaseModel):
    item_code: str
    qty: int = 1
    context: dict | None = None


@router.get("/catalog")
async def catalog(db: AsyncSession = Depends(get_db)):
    return {"items": await get_catalog(db)}


@router.post("/purchase")
async def buy(body: PurchaseRequest, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    try:
        return await purchase(db, user.id, body.item_code, body.qty, body.context)
    except ShopError as e:
        raise HTTPException(status_code=400, detail=str(e))
