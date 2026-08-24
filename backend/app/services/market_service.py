"""Player-facing, visit-scoped caravan market with authoritative shared stock."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.caravan_visit import CARAVAN_VISIBLE_PHASES, CaravanVisit
from app.models.market import CaravanMarketPurchase, LabMarketCandidate
from app.models.resident import Resident
from app.models.shop import Item, Purchase
from app.models.world_event import WorldEvent
from app.services import coin_service
from app.services.caravan_service import IMPORT_DEFS, IMPORT_KIND, IMPORT_STOCK

logger = logging.getLogger(__name__)


class MarketError(Exception):
    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


GOOD_RECEIPTS = {
    "import_tea": "market_tea_chest",
    "import_trinket": "market_trinket_display",
    "import_cloth": "market_cloth_roll",
}

SERVICE_DEFS = (
    {
        "code": "market_appraisal",
        "type": "service",
        "name": "商路作品鉴定",
        "description": "商队鉴定师为你的一件在售居民作品留下到访认证",
        "icon": "🔎",
        "price_sc": 3,
        "stock": 4,
        "effect_key": "appraise_owned_work",
    },
    {
        "code": "market_artisan_lantern",
        "type": "service",
        "name": "异域工匠定制",
        "description": "委托随队工匠制作一盏可摆进家园的限量灯饰",
        "icon": "🏮",
        "price_sc": 7,
        "stock": 2,
        "effect_key": "grant_foreign_lantern",
    },
)
SERVICE_BY_CODE = {definition["code"]: definition for definition in SERVICE_DEFS}
GOOD_BY_CODE = {definition["code"]: definition for definition in IMPORT_DEFS}


async def _rollout_enabled(db: AsyncSession) -> bool:
    """All prerequisites must agree; any rollback gate closes player spending."""
    from app.services.caravan_service import is_caravan_enabled

    return bool(
        settings.market_player_enabled
        and settings.npc_economy_enabled
        and await is_caravan_enabled(db)
        and settings.caravan_lifecycle_enabled
        and settings.item_stock_guard_enabled
        and settings.market_day_venue == "market_hall"
    )


def _belongs_to_visit(item: Item | None, visit_id: str) -> bool:
    return bool(
        item is not None
        and item.active
        and (item.payload_json or {}).get("caravan_visit_id") == visit_id
    )


def _serialize_purchase(row: CaravanMarketPurchase, *, idempotent: bool) -> dict:
    return {
        "ok": True,
        "purchase_id": row.id,
        "visit_id": row.visit_id,
        "offer_code": row.offer_code,
        "offer_type": row.offer_type,
        "qty": int(row.qty),
        "total_sc": int(row.total_sc),
        "effect": row.effect_json or {},
        "idempotent": idempotent,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _visible_visit(db: AsyncSession) -> CaravanVisit | None:
    return (
        await db.execute(
            select(CaravanVisit)
            .where(CaravanVisit.phase.in_(CARAVAN_VISIBLE_PHASES))
            .order_by(CaravanVisit.next_action_at.asc(), CaravanVisit.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _owned_work(db: AsyncSession, user_id: str) -> Item | None:
    slugs = set(
        (
            await db.execute(select(Resident.slug).where(Resident.creator_id == user_id))
        ).scalars()
    )
    if not slugs:
        return None
    works = (
        await db.execute(
            select(Item)
            .where(Item.kind == "resident_work", Item.active.is_(True))
            .order_by(Item.code)
        )
    ).scalars().all()
    return next(
        (
            item for item in works
            if (item.payload_json or {}).get("creator_slug") in slugs
        ),
        None,
    )


async def current_market(db: AsyncSession, *, user_id: str) -> dict:
    visit = await _visible_visit(db)
    enabled = await _rollout_enabled(db)
    if visit is None:
        return {
            "active": False,
            "enabled": enabled,
            "purchase_enabled": False,
            "visit_id": None,
            "phase": None,
            "catalog_version": settings.market_catalog_version,
            "closes_at": None,
            "offers": [],
            "research_candidates": [],
            "message": "商队尚未抵达，集市大厅保留参观开放。",
        }

    event = await db.get(WorldEvent, visit.world_event_id)
    purchase_enabled = enabled and visit.phase == "trading"
    purchased = set(
        (
            await db.execute(
                select(CaravanMarketPurchase.offer_code).where(
                    CaravanMarketPurchase.visit_id == visit.id,
                    CaravanMarketPurchase.user_id == user_id,
                )
            )
        ).scalars()
    )
    items = {
        item.code: item
        for item in (
            await db.execute(
                select(Item).where(Item.code.in_(tuple(GOOD_BY_CODE)))
            )
        ).scalars()
    }
    service_counts = dict(
        (
            await db.execute(
                select(
                    CaravanMarketPurchase.offer_code,
                    func.count(CaravanMarketPurchase.id),
                )
                .where(
                    CaravanMarketPurchase.visit_id == visit.id,
                    CaravanMarketPurchase.offer_code.in_(tuple(SERVICE_BY_CODE)),
                )
                .group_by(CaravanMarketPurchase.offer_code)
            )
        ).all()
    )

    offers: list[dict] = []
    for definition in IMPORT_DEFS:
        item = items.get(definition["code"])
        remaining = int(item.stock or 0) if _belongs_to_visit(item, visit.id) else 0
        bought = definition["code"] in purchased
        offers.append(
            {
                "code": definition["code"],
                "type": "good",
                "name": definition["name"],
                "description": f"{definition['description']}；购买后成为可摆放的家园收藏",
                "icon": definition["icon"],
                "price_sc": int(definition["price_sc"]),
                "stock": remaining,
                "stock_total": IMPORT_STOCK,
                "per_user_limit": 1,
                "purchased": bought,
                "eligible": True,
                "available": purchase_enabled and remaining > 0 and not bought,
                "effect_key": "grant_market_decor",
            }
        )

    owned_work = await _owned_work(db, user_id)
    for definition in SERVICE_DEFS:
        remaining = max(
            0, int(definition["stock"]) - int(service_counts.get(definition["code"], 0))
        )
        bought = definition["code"] in purchased
        eligible = definition["code"] != "market_appraisal" or owned_work is not None
        offers.append(
            {
                **definition,
                "stock": remaining,
                "stock_total": int(definition["stock"]),
                "per_user_limit": 1,
                "purchased": bought,
                "eligible": eligible,
                "unavailable_reason": None if eligible else "你目前没有可鉴定的在售作品",
                "available": purchase_enabled and remaining > 0 and not bought and eligible,
            }
        )

    candidates = (
        await db.execute(
            select(LabMarketCandidate)
            .where(LabMarketCandidate.status == "approved")
            .order_by(LabMarketCandidate.updated_at.desc())
            .limit(6)
        )
    ).scalars().all()
    phase_message = {
        "waiting": "商队正在镇外候场，可先查看本次集市说明。",
        "inbound": "商队正在进镇，商品抵达后开放购买。",
        "trading": "商队已在集市大厅开摊。",
        "outbound": "本次交易已经结束，商队正在离镇。",
    }.get(visit.phase, "集市暂未开放购买。")
    if not enabled:
        phase_message = "玩家交易仍在灰度关闭中；可参观商队和商品预告。"
    return {
        "active": True,
        "enabled": enabled,
        "purchase_enabled": purchase_enabled,
        "visit_id": visit.id,
        "phase": visit.phase,
        "catalog_version": settings.market_catalog_version,
        "closes_at": event.ends_at.isoformat() if event and event.ends_at else None,
        "offers": offers,
        "research_candidates": [
            {
                "id": candidate.id,
                "title": candidate.title,
                "summary": candidate.summary,
                "offer_type": candidate.offer_type,
                "suggested_price_sc": int(candidate.suggested_price_sc),
                "status": candidate.status,
            }
            for candidate in candidates
        ],
        "message": phase_message,
    }


async def _apply_offer(
    db: AsyncSession,
    *,
    visit: CaravanVisit,
    user_id: str,
    offer_code: str,
) -> tuple[str, int, str, dict]:
    """Return ``(offer_type, price, receipt_code, effect)`` with writes pending."""
    if offer_code in GOOD_BY_CODE:
        definition = GOOD_BY_CODE[offer_code]
        item = (
            await db.execute(
                select(Item).where(Item.code == offer_code, Item.kind == IMPORT_KIND)
            )
        ).scalar_one_or_none()
        if not _belongs_to_visit(item, visit.id):
            raise MarketError("商品已经售罄")
        from app.services.item_stock import take_authoritative_stock

        remaining = await take_authoritative_stock(db, item, 1)
        if remaining is None:
            raise MarketError("商品已经售罄")
        receipt = GOOD_RECEIPTS[offer_code]
        return (
            "good",
            int(definition["price_sc"]),
            receipt,
            {
                "kind": "decor_receipt",
                "item_code": receipt,
                "source_item_code": offer_code,
                "stock_remaining": remaining,
            },
        )

    definition = SERVICE_BY_CODE.get(offer_code)
    if definition is None:
        raise MarketError("本次集市没有这个商品或服务", status_code=404)
    sold = (
        await db.execute(
            select(func.count(CaravanMarketPurchase.id)).where(
                CaravanMarketPurchase.visit_id == visit.id,
                CaravanMarketPurchase.offer_code == offer_code,
            )
        )
    ).scalar_one()
    if int(sold or 0) >= int(definition["stock"]):
        raise MarketError("本次服务名额已经用完")

    if offer_code == "market_appraisal":
        work = await _owned_work(db, user_id)
        if work is None:
            raise MarketError("你目前没有可鉴定的在售作品")
        payload = dict(work.payload_json or {})
        payload["market_appraisal"] = {
            "visit_id": visit.id,
            "label": "靛篷商队认证",
            "appraised_at": datetime.now(UTC).isoformat(),
        }
        work.payload_json = payload
        await db.flush()
        return (
            "service",
            int(definition["price_sc"]),
            "",
            {"kind": "appraisal", "item_code": work.code, "item_name": work.name},
        )

    return (
        "service",
        int(definition["price_sc"]),
        "market_foreign_lantern",
        {"kind": "decor_receipt", "item_code": "market_foreign_lantern"},
    )


async def purchase(
    db: AsyncSession,
    *,
    user_id: str,
    visit_id: str,
    offer_code: str,
    request_key: str,
) -> dict:
    existing = (
        await db.execute(
            select(CaravanMarketPurchase).where(
                CaravanMarketPurchase.user_id == user_id,
                CaravanMarketPurchase.request_key == request_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.visit_id != visit_id or existing.offer_code != offer_code:
            raise MarketError("幂等键已用于另一笔交易")
        return _serialize_purchase(existing, idempotent=True)

    if not await _rollout_enabled(db):
        raise MarketError("玩家集市或其前置开关仍在灰度关闭中", status_code=503)

    # The visit row serializes service-cap checks and phase closure against all
    # purchase requests. Lifecycle transitions also update this authority row.
    visit = (
        await db.execute(
            select(CaravanVisit)
            .where(CaravanVisit.id == visit_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if visit is None:
        raise MarketError("商队到访不存在", status_code=404)
    if visit.phase != "trading":
        raise MarketError("本次集市已停止交易")

    already = (
        await db.execute(
            select(CaravanMarketPurchase.id).where(
                CaravanMarketPurchase.visit_id == visit.id,
                CaravanMarketPurchase.user_id == user_id,
                CaravanMarketPurchase.offer_code == offer_code,
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        raise MarketError("每位玩家每次到访限购一次")

    try:
        offer_type, price, receipt_code, effect = await _apply_offer(
            db, visit=visit, user_id=user_id, offer_code=offer_code
        )
        if not await coin_service.charge_pending(
            db, user_id, price, f"market_purchase:{visit.id}:{offer_code}"
        ):
            await db.rollback()
            raise MarketError("Soul Coin 余额不足", status_code=402)

        row = CaravanMarketPurchase(
            visit_id=visit.id,
            user_id=user_id,
            request_key=request_key,
            offer_code=offer_code,
            offer_type=offer_type,
            qty=1,
            total_sc=price,
            effect_json=effect,
        )
        db.add(row)
        if receipt_code:
            db.add(
                Purchase(
                    user_id=user_id,
                    item_code=receipt_code,
                    qty=1,
                    total_sc=price,
                    context_json={
                        "source": "caravan_market",
                        "visit_id": visit.id,
                        "offer_code": offer_code,
                    },
                )
            )
        await db.commit()
        await db.refresh(row)
    except MarketError:
        if db.in_transaction():
            await db.rollback()
        raise
    except IntegrityError:
        await db.rollback()
        replay = (
            await db.execute(
                select(CaravanMarketPurchase).where(
                    CaravanMarketPurchase.user_id == user_id,
                    CaravanMarketPurchase.request_key == request_key,
                )
            )
        ).scalar_one_or_none()
        if replay is not None and replay.visit_id == visit_id and replay.offer_code == offer_code:
            return _serialize_purchase(replay, idempotent=True)
        raise MarketError("交易请求发生并发冲突，请刷新后重试")
    except Exception:
        await db.rollback()
        raise

    try:
        from app.ws.manager import manager

        await manager.broadcast(
            {
                "type": "market_player_purchase",
                "visit_id": visit.id,
                "offer_code": offer_code,
                "offer_type": offer_type,
            }
        )
    except Exception:
        logger.warning("market player purchase broadcast failed", exc_info=True)
    return _serialize_purchase(row, idempotent=False)
