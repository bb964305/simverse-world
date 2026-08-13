"""Restart-safe market-day visitors and resident purchases from the caravan.

The caravan's imported goods are an external-money sink: residents pay from
their real treasury, stock is decremented with a database CAS, and no local
seller is credited.  ``CaravanMarketVisitor`` is both the durable four-person
assignment and the resident's ownership receipt, so retries and worker restarts
cannot charge twice or silently replace the crowd.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.caravan_visit import CaravanMarketVisitor, CaravanVisit
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.models.shop import Item
from app.services import coin_service
from app.services.civic_membership import SIM_RESIDENT_TYPES

logger = logging.getLogger(__name__)

MARKET_VISITOR_LIMIT = 4


def _rank(visit_id: str, resident_id: str) -> bytes:
    return hashlib.sha256(f"{visit_id}\x1f{resident_id}".encode()).digest()


async def ensure_market_visitors(
    db: AsyncSession, visit_id: str, *, now: datetime | None = None,
) -> tuple[CaravanMarketVisitor, ...]:
    """Persist up to four real, funded autonomous visitors for this visit.

    Existing assignments are immutable.  Selection excludes protected live
    activities and residents already inside the hall, then applies a stable
    visit-scoped rank.  A wallet must be able to buy the cheapest import while
    retaining the configured poverty reserve; this makes the four invitations
    honest purchase opportunities rather than decorative actors.

    Flush-owned: the lifecycle transition owns commit/rollback.
    """
    existing = (await db.execute(
        select(CaravanMarketVisitor)
        .where(CaravanMarketVisitor.visit_id == visit_id)
        .order_by(CaravanMarketVisitor.slot_index)
    )).scalars().all()
    if existing:
        return tuple(existing)

    from app.agent.map_data import get_location_id_at
    from app.services.caravan_service import IMPORT_DEFS

    cheapest = min(int(definition["price_sc"]) for definition in IMPORT_DEFS)
    reserve = int(settings.npc_trade_reserve_sc or 0)
    rows = (await db.execute(
        select(
            Resident.id,
            Resident.slug,
            Resident.tile_x,
            Resident.tile_y,
            ResidentTreasury.balance_sc,
        )
        .join(ResidentTreasury, ResidentTreasury.resident_slug == Resident.slug)
        .where(
            Resident.resident_type.in_(SIM_RESIDENT_TYPES),
            # "调拨" only chooses genuinely free residents. Walking covers
            # saved plan trips; work/research/chat/sleep are likewise left
            # untouched instead of being recruited and then silently blocked.
            Resident.status == "idle",
            # Match the established NPC consumption reserve rule (strict >).
            ResidentTreasury.balance_sc > cheapest + reserve,
        )
    )).all()
    candidates = [
        (str(resident_id), str(slug))
        for resident_id, slug, tile_x, tile_y, _balance in rows
        if get_location_id_at(tile_x or 0, tile_y or 0) != "market_hall"
    ]
    chosen = sorted(
        candidates, key=lambda row: _rank(visit_id, row[0])
    )[:MARKET_VISITOR_LIMIT]
    if not chosen:
        return ()

    created_at = now or datetime.now(UTC)
    dialect = db.get_bind().dialect.name
    # Slot identity uses resident-id order so the synchronous agent decision
    # helper can reconstruct it from the cohort without another database read.
    for slot_index, (resident_id, resident_slug) in enumerate(
        sorted(chosen, key=lambda row: row[0])
    ):
        values = {
            "id": str(uuid.uuid4()),
            "visit_id": visit_id,
            "resident_id": resident_id,
            "resident_slug": resident_slug,
            "slot_index": slot_index,
            "created_at": created_at,
        }
        if dialect in ("postgresql", "sqlite"):
            insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
            await db.execute(
                insert(CaravanMarketVisitor).values(**values).on_conflict_do_nothing(
                    index_elements=[
                        CaravanMarketVisitor.visit_id,
                        CaravanMarketVisitor.slot_index,
                    ]
                )
            )
        else:
            db.add(CaravanMarketVisitor(**values))
    await db.flush()
    return tuple((await db.execute(
        select(CaravanMarketVisitor)
        .where(CaravanMarketVisitor.visit_id == visit_id)
        .order_by(CaravanMarketVisitor.slot_index)
    )).scalars().all())


async def assigned_visitor_ids(
    db: AsyncSession, world_event_id: str,
) -> frozenset[str] | None:
    """Read a durable cohort; ``None`` means no inbound/trading visit exists."""
    rows = (await db.execute(
        select(CaravanVisit.id, CaravanMarketVisitor.resident_id)
        .outerjoin(
            CaravanMarketVisitor,
            CaravanMarketVisitor.visit_id == CaravanVisit.id,
        )
        .where(
            CaravanVisit.world_event_id == world_event_id,
            CaravanVisit.phase.in_(["inbound", "trading"]),
        )
        .order_by(CaravanMarketVisitor.slot_index)
    )).all()
    if not rows:
        return None
    return frozenset(
        str(resident_id) for _visit_id, resident_id in rows
        if resident_id is not None
    )


async def _take_import_stock(
    db: AsyncSession, item: Item, visit_id: str,
) -> int | None:
    """CAS one unit from this visit's shelf; caller owns rollback/commit."""
    payload = dict(item.payload_json or {})
    if payload.get("caravan_visit_id") != visit_id:
        return None
    if item.stock is None:
        await db.execute(
            update(Item)
            .where(Item.id == item.id, Item.stock.is_(None))
            .values(stock=int(payload.get("stock") or 0))
            .execution_options(synchronize_session=False)
        )
    remaining = (await db.execute(
        update(Item)
        .where(Item.id == item.id, Item.active.is_(True), Item.stock >= 1)
        .values(stock=Item.stock - 1)
        .returning(Item.stock)
        .execution_options(synchronize_session=False)
    )).scalar_one_or_none()
    if remaining is None:
        return None
    payload["stock"] = int(remaining)
    await db.execute(
        update(Item)
        .where(Item.id == item.id)
        .values(payload_json=payload, active=int(remaining) > 0)
        .execution_options(synchronize_session=False)
    )
    return int(remaining)


def _offer_rank(visit_id: str, resident_id: str, code: str) -> bytes:
    return hashlib.sha256(
        f"{visit_id}\x1f{resident_id}\x1f{code}".encode()
    ).digest()


async def maybe_purchase_for_resident(
    db: AsyncSession, resident: Resident, *, now: datetime | None = None,
) -> dict | None:
    """Buy one real import after an assigned resident reaches its unique slot.

    The visit row lock serializes purchase sequences for WebSocket playback;
    the assignment lock is the idempotency claim.  Debit, stock CAS, receipt,
    sequence, and wallet cache commit atomically.  Any fault rolls back only
    this resident's attempt and leaves the other three visitors independent.
    """
    if not (settings.npc_economy_enabled and settings.caravan_lifecycle_enabled):
        return None
    now = now or datetime.now(UTC)
    try:
        assignment = (await db.execute(
            select(CaravanMarketVisitor)
            .join(CaravanVisit, CaravanVisit.id == CaravanMarketVisitor.visit_id)
            .where(
                CaravanMarketVisitor.resident_id == resident.id,
                CaravanVisit.phase == "trading",
                CaravanVisit.imports_withdrawn_at.is_(None),
            )
            .order_by(CaravanMarketVisitor.created_at.desc())
            .with_for_update()
            .limit(1)
        )).scalars().first()
        if assignment is None or assignment.purchased_at is not None:
            return None

        from app.services.crowd_service import MARKET_DAY_VISITOR_TILES

        target = MARKET_DAY_VISITOR_TILES[int(assignment.slot_index)]
        if (int(resident.tile_x), int(resident.tile_y)) != target:
            return None

        visit = (await db.execute(
            select(CaravanVisit)
            .where(CaravanVisit.id == assignment.visit_id, CaravanVisit.phase == "trading")
            .with_for_update()
        )).scalar_one_or_none()
        if visit is None:
            return None

        balance = await coin_service.treasury_balance(db, resident.slug)
        reserve = int(settings.npc_trade_reserve_sc or 0)
        offers = (await db.execute(
            select(Item).where(
                Item.kind == "import_good", Item.active.is_(True), Item.stock >= 1,
            ).order_by(Item.code)
        )).scalars().all()
        offers = [
            item for item in offers
            if (item.payload_json or {}).get("caravan_visit_id") == visit.id
            and balance > int(item.price_sc) + reserve
        ]
        if not offers:
            return None
        item = min(
            offers,
            key=lambda offer: _offer_rank(visit.id, resident.id, offer.code),
        )
        price = int(item.price_sc)
        if not await coin_service.treasury_debit_with_reserve_pending(
            db, resident.slug, price, minimum_remaining=reserve + 1,
        ):
            return None
        if await _take_import_stock(db, item, visit.id) is None:
            await db.rollback()
            return None

        last_sequence = int((await db.execute(
            select(func.coalesce(func.max(CaravanMarketVisitor.purchase_sequence), 0))
            .where(CaravanMarketVisitor.visit_id == visit.id)
        )).scalar_one())
        sequence = last_sequence + 1
        assignment.item_code = item.code
        assignment.spent_sc = price
        assignment.purchase_sequence = sequence
        assignment.purchased_at = now
        from app.services.duty_service import set_wallet_cache

        set_wallet_cache(
            db, resident,
            await coin_service.treasury_balance(db, resident.slug),
        )
        await db.commit()
        return {
            "type": "market_purchase",
            "visit_id": visit.id,
            "resident_slug": resident.slug,
            "purchase_id": assignment.id,
            "sequence": sequence,
            "item_name": item.name,
            "quantity": 1,
            "amount_sc": price,
        }
    except Exception:
        await db.rollback()
        logger.warning(
            "market purchase failed for resident %s", resident.slug,
            exc_info=True,
        )
        return None
