"""Shop service (S3): catalog lookup + purchase pipeline."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.shop import Item, Purchase
from app.services.coin_service import charge
from app.services.shop_effects import apply_effect


class ShopError(Exception):
    """Raised for purchase failures (router maps to 400)."""


def serialize_item(item: Item) -> dict:
    return {
        "code": item.code,
        "kind": item.kind,
        "name": item.name,
        "description": item.description,
        "icon": item.icon,
        "price_sc": item.price_sc,
        "payload": item.payload_json or {},
        "active": item.active,
    }


async def get_catalog(db: AsyncSession) -> list[dict]:
    result = await db.execute(select(Item).where(Item.active.is_(True)).order_by(Item.price_sc))
    return [serialize_item(i) for i in result.scalars().all()]


async def purchase(
    db: AsyncSession,
    user_id: str,
    item_code: str,
    qty: int = 1,
    context: dict | None = None,
) -> dict:
    """Charge the user and record a purchase, then dispatch the item effect.

    Raises ShopError on unknown/inactive item, bad qty, or insufficient balance
    (charge fails cleanly with no side-effect, so nothing is written).
    """
    if qty < 1:
        raise ShopError("qty must be >= 1")

    item = (await db.execute(select(Item).where(Item.code == item_code))).scalar_one_or_none()
    if item is None or not item.active:
        raise ShopError("Item not available")

    total = item.price_sc * qty
    ok = await charge(db, user_id, total, f"purchase:{item_code}")
    if not ok:
        raise ShopError("Insufficient Soul Coins")

    db.add(Purchase(
        user_id=user_id, item_code=item_code, qty=qty,
        total_sc=total, context_json=context,
    ))
    await db.commit()

    effect = await apply_effect(db, user_id, item, qty, context)
    return {"ok": True, "item_code": item_code, "qty": qty, "total_sc": total, "effect": effect}
