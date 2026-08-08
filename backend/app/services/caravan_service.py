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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.resident import Resident
from app.models.shop import Item
from app.services import coin_service, treasury_service

logger = logging.getLogger(__name__)

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
    cut = await treasury_service.skim_tax_pending(
        db, price, settings.town_tax_rate_sales, f"caravan_tax:{code}")
    earned = price - cut
    if earned > 0:
        await coin_service.treasury_credit_pending(
            db, creator.slug, earned, reason=f"caravan_bought:{code}")

    # 库存扣减:payload_json 没有 mutable 跟踪(app/models/shop.py:23),就地改会被
    # 静默丢弃 —— 整段镜像 shop_effects.py:330-348 的"拷贝→改→整体重赋值"。
    payload = dict(item.payload_json or {})
    stock = int(payload.get("stock", 1)) - 1
    payload["stock"] = max(0, stock)
    item.payload_json = payload
    if stock <= 0:
        item.active = False

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
            db.add(Item(**d, kind=IMPORT_KIND, payload_json=dict(payload), active=True))
        else:
            existing.active = True
            existing.payload_json = dict(payload)
            existing.price_sc = d["price_sc"]
    await db.commit()
    return len(IMPORT_DEFS)


async def run_caravan_visit(db: AsyncSession, event) -> dict:
    """一次商队到访。返回 `{"bought", "spent", "tax", "fee", "imported"}` 摘要。

    由 `event_cron` 在集市日事件 `phase=="start"` 时调用(判据在调用点,这里只管
    到访本身)。`npc_economy_enabled` + `caravan_enabled` 双闸,关 → 零 DB 写入。
    """
    summary = {"bought": 0, "spent": 0, "tax": 0, "fee": 0, "imported": 0}
    if not (settings.npc_economy_enabled and settings.caravan_enabled):
        return summary

    event_id = _event_id(event)
    if not event_id:
        logger.warning("caravan visit skipped: event has no id")
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
