"""Shop endpoints (S3): catalog + purchase (+ D2 inventory)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.shop import Item, Purchase
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


@router.get("/inventory")
async def inventory(request: Request, db: AsyncSession = Depends(get_db)):
    """My purchases aggregated by item (D2 库存 — decor lives here until B3)."""
    user = await _require_user(request, db)
    rows = (await db.execute(
        select(Purchase.item_code, func.sum(Purchase.qty), func.sum(Purchase.total_sc))
        .where(Purchase.user_id == user.id)
        .group_by(Purchase.item_code)
    )).all()
    items: dict[str, Item] = {}
    codes = [r[0] for r in rows]
    if codes:
        items = {i.code: i for i in (await db.execute(
            select(Item).where(Item.code.in_(codes))
        )).scalars()}
    return {"inventory": [
        {
            "item_code": code,
            "qty": int(qty or 0),
            "total_sc": int(total or 0),
            "name": items[code].name if code in items else code,
            "icon": items[code].icon if code in items else "📦",
            "kind": items[code].kind if code in items else "",
        }
        for code, qty, total in rows
    ]}


@router.post("/purchase")
async def buy(body: PurchaseRequest, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    try:
        return await purchase(db, user.id, body.item_code, body.qty, body.context)
    except ShopError as e:
        raise HTTPException(status_code=400, detail=str(e))
