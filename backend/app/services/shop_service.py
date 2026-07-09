"""Shop service (S3): catalog lookup + purchase pipeline."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.shop import Item, Purchase
from app.services.coin_service import charge
from app.services.shop_effects import apply_effect, precheck_effect


class ShopError(Exception):
    """Raised for purchase failures (router maps to 400)."""


# First items (D2). Seeded idempotently at startup so the catalog is populated.
ITEM_DEFS: list[dict] = [
    {"code": "rename_card", "kind": "consumable", "name": "改名卡", "description": "给你的居民改个名字", "icon": "✏️", "price_sc": 50, "payload_json": {}},
    {"code": "portrait_redraw", "kind": "consumable", "name": "肖像重绘", "description": "为居民重新生成 AI 肖像", "icon": "🎨", "price_sc": 80, "payload_json": {}},
    {"code": "gift_flower", "kind": "gift", "name": "一束花", "description": "送给居民，增进关系", "icon": "💐", "price_sc": 15, "payload_json": {"relationship_boost": 0.1}},
    {"code": "gift_book", "kind": "gift", "name": "一本书", "description": "送给居民，增进关系", "icon": "📖", "price_sc": 25, "payload_json": {"relationship_boost": 0.15}},
    {"code": "gift_snack", "kind": "gift", "name": "一份点心", "description": "送给居民，增进关系", "icon": "🍰", "price_sc": 10, "payload_json": {"relationship_boost": 0.08}},
    {"code": "decor_lamp", "kind": "decor", "name": "落地灯", "description": "家园装饰", "icon": "🪔", "price_sc": 30, "payload_json": {"sprite": "lamp_01", "w": 1, "h": 1}},
    {"code": "decor_plant", "kind": "decor", "name": "盆栽", "description": "家园装饰", "icon": "🪴", "price_sc": 40, "payload_json": {"sprite": "plant_01", "w": 1, "h": 1}},
    {"code": "decor_rug", "kind": "decor", "name": "地毯", "description": "家园装饰", "icon": "🟫", "price_sc": 60, "payload_json": {"sprite": "rug_01", "w": 2, "h": 2}},
    {"code": "tip_5sc", "kind": "tip", "name": "打赏 5", "description": "给创作打赏 5 SC", "icon": "💰", "price_sc": 5, "payload_json": {}},
    {"code": "tip_20sc", "kind": "tip", "name": "打赏 20", "description": "给创作打赏 20 SC", "icon": "💎", "price_sc": 20, "payload_json": {}},
]


async def seed_items(db: AsyncSession) -> int:
    """Upsert ITEM_DEFS into the items table (skips existing codes)."""
    for d in ITEM_DEFS:
        existing = (await db.execute(select(Item).where(Item.code == d["code"]))).scalar_one_or_none()
        if existing is None:
            db.add(Item(**d, active=True))
    await db.commit()
    return len(ITEM_DEFS)


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

    # Pre-charge validation (e.g. rename sensitive-word check) — raises ShopError
    # before any coins are debited.
    await precheck_effect(db, user_id, item, qty, context)

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
