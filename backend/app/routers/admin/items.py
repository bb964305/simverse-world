"""Admin catalog management (S3) — require_admin on all routes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.shop import Item
from app.models.user import User
from app.routers.admin.middleware import require_admin
from app.services.shop_service import serialize_item

router = APIRouter(prefix="/items", tags=["admin-items"])


class ItemCreate(BaseModel):
    code: str
    kind: str = "consumable"
    name: str
    description: str = ""
    icon: str = "📦"
    price_sc: int = 0
    payload_json: dict = {}
    active: bool = True


class ItemUpdate(BaseModel):
    kind: str | None = None
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    price_sc: int | None = None
    payload_json: dict | None = None
    active: bool | None = None


@router.get("")
async def list_items(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Item).order_by(Item.price_sc))
    return {"items": [serialize_item(i) for i in result.scalars().all()]}


@router.post("")
async def create_item(
    body: ItemCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not body.code.strip():
        raise HTTPException(status_code=400, detail="code is required")
    existing = (await db.execute(select(Item).where(Item.code == body.code))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="code already exists")
    item = Item(**body.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return serialize_item(item)


@router.patch("/{code}")
async def update_item(
    code: str,
    body: ItemUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    item = (await db.execute(select(Item).where(Item.code == code))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    await db.commit()
    await db.refresh(item)
    return serialize_item(item)
