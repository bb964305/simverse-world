"""M-A C4 — 外来商队(绑集市日):镇外的买方与卖方,一次到访只准来一次。

这套经济里所有别的钱流都是内部转移(买方付、卖方收、镇库抽成),总量只减不增。
商队是唯一的**外生**面:它收购居民作品(注入 = 贸易顺差,铸币是设计不是缺陷)、
交摊位费(第二税源,不依赖 tax_rate)、摆出进口货让居民买(sink,通胀对冲)。

至多一次(at-most-once)是本模块的第一纪律:`system_config` 的
`caravan_last_event_id` 在**任何资金动作之前**先写并 commit——中途崩溃宁可丢半次
到访,也绝不重复收费/重复收购。同一个 event id 再进来一次就是直接返回。

事务纪律同 C2(`npc_trade_service`):
- 一件收购 = **一个事务**:`skim_tax_pending` + `treasury_credit_pending` + 库存
  重赋值 + 作者钱包缓存 → 单次 `commit`;中途炸掉先 `rollback` 再进下一件,否则
  悬挂的半笔账会被后面某一件的 commit 带落库。
- memory / feed 一律放 commit **之后**并 fail-open(`add_memory` 自带 commit)。
- 扫描阶段取标量行(plain tuple):rollback 会 expire 整个 session 的 ORM 对象。

进口货只卖给居民(C2 的标的之一)——**玩家不可见**:`shop_service.get_catalog`
排除该 kind、玩家 `purchase` 该 kind 直接被拒,否则玩家花真钱买到一口空气
(`import_good` 没有 shop_effects handler,apply_effect 返 None 也不补偿)。
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.resident import Resident
from app.models.caravan_visit import CaravanVisit, CaravanVisitPurchase
from app.models.shop import Item
from app.services import coin_service, treasury_service

logger = logging.getLogger(__name__)


def _wall_now() -> datetime:
    """Injectable lease clock; financial tests can advance it deterministically."""
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

# 幂等键:上一次已经跑完到访动作的世界事件 id(裸 upsert,不走会内部 commit 的
# ConfigService.set)。
LAST_VISIT_KEY = "caravan_last_event_id"

IMPORT_KIND = "import_good"

# 商队摆出来的货:定价压在居民作品(15 SC)之下,买得起的人更多;stock 每次到访
# 重置成 2,卖不完也不会越堆越多。
IMPORT_DEFS: list[dict] = [
    {"code": "import_tea", "name": "茶叶", "icon": "🍵",
     "description": "商队从远处捎来的茶叶", "price_sc": 6},
    {"code": "import_trinket", "name": "小玩意", "icon": "🎁",
     "description": "商队货箱底下翻出来的小玩意", "price_sc": 4},
    {"code": "import_cloth", "name": "花布", "icon": "🧵",
     "description": "商队带来的外乡花布", "price_sc": 8},
]
IMPORT_STOCK = 2


async def is_caravan_enabled(db: AsyncSession) -> bool:
    """Resolve the binding policy, with the env switch as rollback fallback."""
    fallback = bool(settings.caravan_enabled)
    if not settings.polis_policy_enabled:
        return fallback
    try:
        from app.services.policy_service import PolicyService
        value = await PolicyService(db).get("caravan_enabled", default=fallback)
        if isinstance(value, bool):
            return value
        logger.warning(
            "invalid caravan_enabled policy value %r; using env fallback=%s",
            value, fallback,
        )
    except Exception:
        logger.warning(
            "caravan policy lookup failed; using env fallback", exc_info=True,
        )
    return fallback


def _event_id(event) -> str | None:
    """`flip_active_events` 交出来的是 dict(world_event_service.py:105);ORM 对象
    也认一手,免得换个调用来源就把幂等键静默丢成 None。"""
    if isinstance(event, dict):
        return event.get("id")
    return getattr(event, "id", None)


async def _pay_stall_fee(db: AsyncSession) -> int:
    """摊位费入镇库(第二税源,固定额、不依赖 tax_rate)。镇库闸关则跳过。"""
    fee = int(settings.caravan_stall_fee_sc or 0)
    if fee <= 0 or not settings.town_treasury_enabled:
        return 0
    try:
        await treasury_service.tax_pending(db, fee, "caravan_stall_fee")
        await db.commit()
        return fee
    except Exception:
        await db.rollback()
        logger.warning("caravan stall fee failed", exc_info=True)
        return 0


async def _on_sale(db: AsyncSession) -> list[tuple[str, int, str | None]]:
    """在售作品(按 code 稳定序)+ 作者 slug。返回标量行,不是 ORM。"""
    rows = (await db.execute(
        select(Item.code, Item.price_sc, Item.payload_json)
        .where(Item.active.is_(True), Item.kind == "resident_work")
        .order_by(Item.code)
    )).all()
    return [(r.code, r.price_sc, (r.payload_json or {}).get("creator_slug"))
            for r in rows]


async def _delist(db: AsyncSession, code: str) -> None:
    """孤儿作品下架:作者已被 purge,这货买了也没人收钱(vm212 有存量)。"""
    await db.execute(
        update(Item).where(Item.code == code).values(active=False)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    logger.info("caravan delisted orphan work %s", code)


async def _narrate(db: AsyncSession, creator: Resident, *, code: str, name: str,
                   earned: int) -> None:
    """commit 之后的叙事面(memory + feed),整段 fail-open——钱已经落库了,一条
    记忆写不进去不该把这笔买卖连坐掉(`add_memory` 自带 commit,放进事务里会把单
    事务撕成两半)。"""
    from app.memory.service import MemoryService
    from app.services import feed_service

    try:
        await MemoryService(db).add_memory(
            creator.id, "event",
            f"外来的商队买走了我的「{name}」,给了 {earned} 枚硬币。"
            "东西要卖到镇子外面去了。",
            0.6, "observation",
        )
    except Exception:
        logger.warning("caravan memory failed for %s", code, exc_info=True)

    try:
        await feed_service.push(creator.slug, "caravan_purchase", {
            "item": code, "name": name, "earned": earned,
        })
    except Exception:
        logger.warning("caravan feed failed for %s", code, exc_info=True)


async def _buy_one(db: AsyncSession, code: str, creator_slug: str | None,
                   summary: dict) -> int:
    """收购一件 = 一个事务。返回实际花掉的 SC(没成交是 0)。"""
    from app.services.duty_service import set_wallet_cache

    item = (await db.execute(select(Item).where(Item.code == code))).scalar_one_or_none()
    if item is None or not item.active:
        return 0
    creator = None
    if creator_slug:
        creator = (await db.execute(
            select(Resident).where(Resident.slug == creator_slug)
        )).scalar_one_or_none()
    if creator is None:
        await _delist(db, code)
        return 0

    price = item.price_sc
    # M-A 加固:库存守卫先行 —— 抢不到货(玩家刚买走最后一件)就一分钱都不动。
    # 这里**还什么都没写**,直接返回,不 rollback(rollback 会 expire 整个
    # session,treasury_service 模块头军规 2)。
    from app.services import item_stock
    if await item_stock.take_stock(db, item, 1) is None:
        logger.info("caravan lost the race for %s (sold out under the guard)", code)
        return 0

    cut = await treasury_service.skim_tax_pending(
        db, price, settings.town_tax_rate_sales, f"caravan_tax:{code}")
    earned = price - cut
    if earned > 0:
        await coin_service.treasury_credit_pending(
            db, creator.slug, earned, reason=f"caravan_bought:{code}")

    # 钱包缓存(prompt 读的那份)——事务内 SELECT 看得到自己尚未 commit 的改动。
    set_wallet_cache(db, creator, await coin_service.treasury_balance(db, creator.slug))
    await db.commit()

    summary["bought"] += 1
    summary["spent"] += price
    summary["tax"] += cut
    await _narrate(db, creator, code=code, name=item.name, earned=earned)
    return price


async def _buy_local_works(db: AsyncSession, summary: dict) -> None:
    """按 code 稳定序逐件买 1,直到预算买不动为止(买不起的那件跳过,后面便宜的
    还有机会)。"""
    budget = int(settings.caravan_budget_sc or 0)
    for code, price, creator_slug in await _on_sale(db):
        if budget <= 0:
            break
        if price > budget:
            continue
        try:
            budget -= await _buy_one(db, code, creator_slug, summary)
        except Exception:
            # 写了半截就必须就地回滚:悬挂的账会被下一件的 commit 带落库。
            await db.rollback()
            logger.warning("caravan purchase failed for %s", code, exc_info=True)
            continue


async def _stock_import_goods(db: AsyncSession) -> int:
    """摆摊:幂等 upsert 三件进口货。已存在的走复活模式(镜像
    `duty_service._maybe_list_resident_work`:412-415 —— active 置回 True、payload
    与定价整体重赋值),不新建重复 code。"""
    payload = {"caravan": True, "stock": IMPORT_STOCK}
    for d in IMPORT_DEFS:
        existing = (await db.execute(
            select(Item).where(Item.code == d["code"]))).scalar_one_or_none()
        if existing is None:
            db.add(Item(**d, kind=IMPORT_KIND, payload_json=dict(payload),
                        stock=IMPORT_STOCK, active=True))
        else:
            existing.active = True
            existing.payload_json = dict(payload)
            existing.stock = IMPORT_STOCK   # M-A 加固:列是真相,payload 是镜像
            existing.price_sc = d["price_sc"]
    await db.commit()
    return len(IMPORT_DEFS)


async def run_caravan_visit(db: AsyncSession, event) -> dict:
    """一次商队到访。返回 `{"bought", "spent", "tax", "fee", "imported"}` 摘要。

    由 `event_cron` 在集市日事件 `phase=="start"` 时调用(判据在调用点,这里只管
    到访本身)。`npc_economy_enabled` + policy-backed `caravan_enabled` 双闸,
    关 → 零 DB 写入。
    """
    summary = {"bought": 0, "spent": 0, "tax": 0, "fee": 0, "imported": 0}
    if not (settings.npc_economy_enabled and await is_caravan_enabled(db)):
        return summary

    event_id = _event_id(event)
    if not event_id:
        logger.warning("caravan visit skipped: event has no id")
        return summary
    # A lifecycle row is a durable ownership fence even if ops subsequently
    # turns its dark gate off.  Falling back to the legacy one-shot path for the
    # same event would mint/pay/stock a second time when the lifecycle resumes.
    durable_visit = (await db.execute(
        select(CaravanVisit.id).where(CaravanVisit.world_event_id == event_id)
    )).scalar_one_or_none()
    if durable_visit is not None:
        return summary
    if await treasury_service.kv_read(db, LAST_VISIT_KEY) == event_id:
        return summary      # 这一场集市已经来过了

    # at-most-once:幂等标记必须先于任何资金动作落库。后面的段落崩了顶多丢半次
    # 到访(下个集市日再来),而重复收费/重复收购是不可逆的钱。
    await treasury_service.kv_upsert_pending(
        db, LAST_VISIT_KEY, event_id, updated_by="caravan_visit")
    await db.commit()

    summary["fee"] = await _pay_stall_fee(db)
    await _buy_local_works(db, summary)
    summary["imported"] = await _stock_import_goods(db)
    logger.info("caravan visit %s: bought=%d spent=%d tax=%d fee=%d imported=%d",
                event_id, summary["bought"], summary["spent"], summary["tax"],
                summary["fee"], summary["imported"])
    return summary


# --------------------------------------------------------------------------- #
# Durable lifecycle settlement (CARAVAN_LIFECYCLE_ENABLED)                    #
# --------------------------------------------------------------------------- #


class CaravanLeaseLost(RuntimeError):
    """The caller no longer owns the visit; its whole current txn must abort."""


async def _renew_owned_visit(
    db: AsyncSession, visit_id: str, owner: str, *, now: datetime,
) -> CaravanVisit:
    """Lock and renew an owned row before a financial transaction.

    The CAS uses both owner and version.  A restarted worker can reclaim an
    expired lease, bump ``version`` and make an old worker's next step a no-op.
    """
    visit = await db.get(CaravanVisit, visit_id, populate_existing=True)
    if visit is None or visit.lease_owner != owner:
        raise CaravanLeaseLost(visit_id)
    lease_base = max(_aware_utc(now), _aware_utc(_wall_now()))
    lease_target = lease_base + timedelta(seconds=settings.caravan_lease_seconds)
    if visit.lease_expires_at is not None:
        lease_target = max(lease_target, _aware_utc(visit.lease_expires_at))
    result = await db.execute(
        update(CaravanVisit)
        .where(
            CaravanVisit.id == visit_id,
            CaravanVisit.version == visit.version,
            CaravanVisit.lease_owner == owner,
        )
        .values(lease_expires_at=lease_target)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise CaravanLeaseLost(visit_id)
    return visit


async def _claim_purchase_row(
    db: AsyncSession, *, visit_id: str, item: Item, creator_slug: str,
) -> str | None:
    """Insert the per-item idempotency record before touching stock or money."""
    purchase_id = str(uuid.uuid4())
    values = {
        "id": purchase_id,
        "visit_id": visit_id,
        "item_code": item.code,
        "creator_slug": creator_slug,
        "qty": 1,
        "gross_sc": int(item.price_sc),
        "tax_sc": 0,
        "net_sc": int(item.price_sc),
        "created_at": datetime.now(UTC),
    }
    dialect = db.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
        result = await db.execute(
            insert(CaravanVisitPurchase)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    CaravanVisitPurchase.visit_id,
                    CaravanVisitPurchase.item_code,
                ]
            )
        )
        return purchase_id if result.rowcount == 1 else None
    existing = (await db.execute(
        select(CaravanVisitPurchase.id).where(
            CaravanVisitPurchase.visit_id == visit_id,
            CaravanVisitPurchase.item_code == item.code,
        )
    )).scalar_one_or_none()
    if existing:
        return None
    db.add(CaravanVisitPurchase(**values))
    await db.flush()
    return purchase_id


async def _take_lifecycle_stock(db: AsyncSession, item: Item) -> int | None:
    """Always use the real ``items.stock`` CAS, independent of the legacy gate."""
    if item.stock is None:
        seed_stock = int((item.payload_json or {}).get("stock") or 0)
        await db.execute(
            update(Item)
            .where(Item.id == item.id, Item.stock.is_(None))
            .values(stock=seed_stock)
            .execution_options(synchronize_session=False)
        )
    result = await db.execute(
        update(Item)
        .where(Item.id == item.id, Item.active.is_(True), Item.stock >= 1)
        .values(stock=Item.stock - 1)
        .returning(Item.stock)
        .execution_options(synchronize_session=False)
    )
    remaining = result.scalar_one_or_none()
    if remaining is None:
        return None
    payload = dict(item.payload_json or {})
    payload["stock"] = int(remaining)
    item.payload_json = payload
    item.active = remaining > 0
    return int(remaining)


async def _buy_one_for_visit(
    db: AsyncSession, visit_id: str, owner: str, code: str, *, now: datetime,
) -> bool:
    """Commit one purchase atomically, guarded by visit/item uniqueness and stock."""
    from app.services.duty_service import set_wallet_cache

    await _renew_owned_visit(db, visit_id, owner, now=now)
    item = (await db.execute(select(Item).where(Item.code == code))).scalar_one_or_none()
    if item is None or not item.active:
        await db.rollback()
        return False
    creator_slug = (item.payload_json or {}).get("creator_slug")
    creator = None
    if creator_slug:
        creator = (await db.execute(
            select(Resident).where(Resident.slug == creator_slug)
        )).scalar_one_or_none()
    if creator is None:
        item.active = False
        await db.commit()
        logger.info("caravan delisted orphan work %s", code)
        return False

    purchase_id = await _claim_purchase_row(
        db, visit_id=visit_id, item=item, creator_slug=creator.slug,
    )
    if purchase_id is None:
        await db.rollback()
        return False
    if await _take_lifecycle_stock(db, item) is None:
        await db.rollback()  # also removes the pending purchase claim
        return False

    price = int(item.price_sc)
    cut = await treasury_service.skim_tax_pending(
        db, price, settings.town_tax_rate_sales,
        f"caravan_tax:{visit_id}:{code}",
    )
    earned = price - cut
    if earned:
        await coin_service.treasury_credit_pending(
            db, creator.slug, earned, reason=f"caravan_bought:{visit_id}:{code}"
        )
    set_wallet_cache(db, creator, await coin_service.treasury_balance(db, creator.slug))
    await db.execute(
        update(CaravanVisitPurchase)
        .where(CaravanVisitPurchase.id == purchase_id)
        .values(tax_sc=cut, net_sc=earned)
        .execution_options(synchronize_session=False)
    )
    # Renew again at transaction end: if stock/tax/wallet work was slow, the
    # committed lease must still extend from the actual commit-side wall clock.
    await _renew_owned_visit(db, visit_id, owner, now=now)
    await db.commit()
    await _narrate(db, creator, code=code, name=item.name, earned=earned)
    return True


async def _stock_import_goods_pending(db: AsyncSession, visit_id: str) -> int:
    payload = {"caravan": True, "caravan_visit_id": visit_id, "stock": IMPORT_STOCK}
    for definition in IMPORT_DEFS:
        existing = (await db.execute(
            select(Item).where(Item.code == definition["code"])
        )).scalar_one_or_none()
        if existing is None:
            db.add(Item(
                **definition, kind=IMPORT_KIND, payload_json=dict(payload),
                stock=IMPORT_STOCK, active=True,
            ))
        else:
            existing.active = True
            existing.payload_json = dict(payload)
            existing.stock = IMPORT_STOCK
            existing.price_sc = definition["price_sc"]
    await db.flush()
    return len(IMPORT_DEFS)


async def settle_caravan_visit(
    db: AsyncSession, visit_id: str, owner: str, *, now: datetime | None = None,
) -> dict:
    """Resume the visit settlement; every material item is independently idempotent."""
    now = now or datetime.now(UTC)
    visit = await _renew_owned_visit(db, visit_id, owner, now=now)
    if visit.phase != "trading":
        await db.rollback()
        raise ValueError(f"visit {visit_id} is not trading")

    if visit.fee_settled_at is None:
        fee = int(settings.caravan_stall_fee_sc or 0)
        if fee > 0 and settings.town_treasury_enabled:
            await treasury_service.tax_pending(
                db, fee, "caravan_stall_fee",
                ref_key=f"caravan:{visit_id}:stall_fee",
            )
            visit.fee_sc = fee
        visit.fee_settled_at = now
        await _renew_owned_visit(db, visit_id, owner, now=now)
        await db.commit()

    spent = int((await db.execute(
        select(func.coalesce(func.sum(CaravanVisitPurchase.gross_sc), 0)).where(
            CaravanVisitPurchase.visit_id == visit_id
        )
    )).scalar_one())
    for code, price, _ in await _on_sale(db):
        if spent + int(price) > int(settings.caravan_budget_sc or 0):
            continue
        try:
            if await _buy_one_for_visit(db, visit_id, owner, code, now=now):
                spent += int(price)
        except CaravanLeaseLost:
            raise
        except Exception:
            await db.rollback()
            logger.warning("durable caravan purchase failed for %s", code, exc_info=True)

    visit = await _renew_owned_visit(db, visit_id, owner, now=now)
    if visit.imports_stocked_at is None:
        await _stock_import_goods_pending(db, visit_id)
        visit.imports_stocked_at = now
        await _renew_owned_visit(db, visit_id, owner, now=now)
        await db.commit()

    visit = await _renew_owned_visit(db, visit_id, owner, now=now)
    rows = (await db.execute(
        select(CaravanVisitPurchase).where(CaravanVisitPurchase.visit_id == visit_id)
    )).scalars().all()
    summary = {
        "fee_sc": int(visit.fee_sc or 0),
        "bought": len(rows),
        "spent_sc": sum(int(row.gross_sc) for row in rows),
        "tax_sc": sum(int(row.tax_sc) for row in rows),
        "imports_stocked": len(IMPORT_DEFS) if visit.imports_stocked_at else 0,
    }
    visit.summary_json = summary
    visit.settled_at = visit.settled_at or now
    await _renew_owned_visit(db, visit_id, owner, now=now)
    await db.commit()
    return summary


async def withdraw_visit_imports(
    db: AsyncSession, visit_id: str, owner: str, *, now: datetime | None = None,
) -> int:
    """Close only this visit's remaining import shelves; sold stock stays audited."""
    now = now or datetime.now(UTC)
    visit = await _renew_owned_visit(db, visit_id, owner, now=now)
    if visit.imports_withdrawn_at is not None:
        await db.commit()  # persist the lease renewal; this replay is a no-op
        return 0
    withdrawn = 0
    for code in (definition["code"] for definition in IMPORT_DEFS):
        item = (await db.execute(select(Item).where(Item.code == code))).scalar_one_or_none()
        if item is None:
            continue
        payload = item.payload_json or {}
        if payload.get("caravan_visit_id") == visit_id and item.active:
            item.active = False
            withdrawn += 1
    rows = (await db.execute(
        select(CaravanVisitPurchase).where(CaravanVisitPurchase.visit_id == visit_id)
    )).scalars().all()
    visit.summary_json = {
        "fee_sc": int(visit.fee_sc or 0),
        "bought": len(rows),
        "spent_sc": sum(int(row.gross_sc) for row in rows),
        "tax_sc": sum(int(row.tax_sc) for row in rows),
        "imports_stocked": len(IMPORT_DEFS) if visit.imports_stocked_at else 0,
    }
    visit.imports_withdrawn_at = now
    await _renew_owned_visit(db, visit_id, owner, now=now)
    await db.commit()
    return withdrawn
