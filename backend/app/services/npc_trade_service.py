"""M-A C2 — NPC 夜间消费 pass:居民之间的真实钱流(零 LLM 规则引擎)。

需求端在现状里整个外包给了玩家:作品市场、委托、打赏的需求侧全是 `user_id` 硬
键,玩家一断流三个市场同时归零。这个模块补的是**居民自己会花钱**——选货、成交、
纳税全是规则(仿 `civic_service._npc_choice` 的打分口径),不进任何 prompt,可关断
(`npc_economy_enabled` + `npc_trade_enabled` 双闸,默认关 → 零 DB 写入)。

事务纪律(本里程碑的头号故障面):
- 一笔成交 = **一个事务**:`treasury_debit_pending` + `skim_tax_pending` +
  `treasury_credit_pending` + 库存扣减 + 双方钱包缓存 → 单次 `commit`。中途任何一
  步炸掉都必须 `rollback` 后再进下一个买方,否则悬挂的半笔 debit 会被后续无关
  commit 落库烧钱。
- memory / feed 一律放 commit **之后**并 fail-open(`MemoryService.add_memory` 自带
  commit,放进事务里会把单事务撕成两半)。
- rollback 会 expire 整个 session 的 ORM 对象(异步下再读属性就是 MissingGreenlet),
  所以扫描阶段一律取标量行(plain tuple),ORM 对象只在成交事务内现查现用。
"""
from __future__ import annotations

import hashlib
import logging
import random

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.resident import Resident
from app.models.shop import Item
from app.services import coin_service, relation_service, treasury_service

logger = logging.getLogger(__name__)

# C2 的两类标的:居民作品(内部转移 + 税)与商队进口货(sink,通胀对冲)。
_TRADABLE_KINDS = ("resident_work", "import_good")
# 本地作品优先于进口货(spec §4 C2):偏置压过 [0, 0.5) 的口味哈希,不压过好感。
_LOCAL_BIAS = 0.5


def _stable_unit(*parts: object) -> float:
    """Deterministic float in ``[0, 1)`` from a stable digest of *parts*.

    5 行复制自 ``civic_service._stable_unit``(civic_service.py:306,模块私有)。
    同样的理由:不用 :func:`hash`(PYTHONHASHSEED 加盐,进程间不同)也不用
    :mod:`random`(不可复现)——同样的买方 × 同样的商品,在任何机器任何一轮都得出
    同一个"口味",挑货才可审计。
    """
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") / 2 ** 64


async def _offers(db: AsyncSession) -> list[dict]:
    """在售标的 + 作者解析(一次 select 建 map),顺手下架孤儿作品。

    item 的生命周期与居民解耦(vm212 有存量孤儿:作者已被 purge、作品还挂着),这种
    货买了没人收钱,直接 `active=False` 摘掉。返回的是 plain dict,不是 ORM ——
    单笔失败的 rollback 会 expire ORM 对象。
    """
    rows = (await db.execute(
        select(Item.code, Item.kind, Item.name, Item.price_sc, Item.payload_json)
        .where(Item.active.is_(True), Item.kind.in_(_TRADABLE_KINDS))
        .order_by(Item.code)
    )).all()

    slugs = {(r.payload_json or {}).get("creator_slug") for r in rows
             if r.kind == "resident_work"}
    slugs.discard(None)
    makers: dict[str, str] = {}
    if slugs:
        makers = dict((await db.execute(
            select(Resident.slug, Resident.id).where(Resident.slug.in_(slugs))
        )).all())

    offers: list[dict] = []
    orphans: list[str] = []
    for r in rows:
        creator_slug = (r.payload_json or {}).get("creator_slug")
        creator_id = makers.get(creator_slug) if creator_slug else None
        if r.kind == "resident_work" and creator_id is None:
            orphans.append(r.code)
            continue
        offers.append({
            "code": r.code, "kind": r.kind, "name": r.name, "price": r.price_sc,
            "creator_slug": creator_slug if r.kind == "resident_work" else None,
            "creator_id": creator_id if r.kind == "resident_work" else None,
        })

    if orphans:
        await db.execute(
            update(Item).where(Item.code.in_(orphans)).values(active=False)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        logger.info("npc_trade delisted %d orphan work(s): %s", len(orphans), orphans)
    return offers


async def _score(db: AsyncSession, buyer_id: str, buyer_slug: str, offer: dict) -> float:
    """打分:底分 + 对作者的好感 + 稳定口味哈希(+ 本地作品偏置)。

    好感取正值面(负好感只是"不加分",不至于让人买不到东西);进口货没有作者,
    好感恒 0。
    """
    affinity = 0.0
    if offer["creator_id"]:
        pair = await relation_service.get_pair(db, buyer_id, offer["creator_id"])
        if pair is not None:
            affinity = max(0.0, float(pair.affinity or 0.0))
    score = 1.0 + affinity + 0.5 * _stable_unit(buyer_slug, offer["code"])
    if offer["kind"] == "resident_work":
        score += _LOCAL_BIAS
    return score


async def _narrate(buyer_id: str, buyer_slug: str, offer: dict, *,
                   price: int, earned: int, creator_name: str | None,
                   creator_id: str | None, creator_slug: str | None,
                   db: AsyncSession) -> None:
    """commit 之后的叙事面(memory + feed),整段 fail-open——钱已经落库了,一条
    记忆写不进去不该把这笔交易连坐掉。"""
    from app.memory.service import MemoryService
    from app.services import feed_service

    name = offer["name"]
    try:
        if offer["kind"] == "resident_work":
            note = f"从{creator_name}那儿买下了「{name}」,花了 {price} 枚硬币。"
            await MemoryService(db).add_memory(
                buyer_id, "event", note, 0.5, "npc_trade",
                related_resident_id=creator_id,
            )
            # 作者侧措辞与玩家购买路径同源(shop_effects.py:358-366)——同一件事
            # 在居民记忆里不该有两种说法。
            await MemoryService(db).add_memory(
                creator_id, "event",
                f"我的「{name}」被人买走了,挣了 {earned} 枚硬币。有人喜欢我做的东西,真好。",
                0.6, "observation", related_resident_id=buyer_id,
            )
        else:
            await MemoryService(db).add_memory(
                buyer_id, "event",
                f"从商队的摊位上买了「{name}」,花了 {price} 枚硬币。", 0.4, "npc_trade",
            )
    except Exception:
        logger.warning("npc_trade memory failed for %s/%s", buyer_slug, offer["code"],
                       exc_info=True)

    try:
        await feed_service.push(buyer_slug, "npc_purchase", {
            "item": offer["code"], "name": name, "price": price,
            "creator": creator_slug, "role": "buyer",
        })
        if creator_slug:
            await feed_service.push(creator_slug, "npc_purchase", {
                "item": offer["code"], "name": name, "earned": earned,
                "buyer": buyer_slug, "role": "seller",
            })
    except Exception:
        logger.warning("npc_trade feed failed for %s/%s", buyer_slug, offer["code"],
                       exc_info=True)


async def _buy(db: AsyncSession, buyer_id: str, buyer_slug: str, offer: dict,
               summary: dict) -> bool:
    """一笔成交 = 一个事务。返回是否真的成交(钱已落库)。"""
    from app.services.duty_service import set_wallet_cache

    code, price, kind = offer["code"], offer["price"], offer["kind"]
    item = (await db.execute(select(Item).where(Item.code == code))).scalar_one_or_none()
    if item is None or not item.active:
        return False
    buyer = (await db.execute(
        select(Resident).where(Resident.slug == buyer_slug)
    )).scalar_one_or_none()
    if buyer is None:
        return False

    if not await coin_service.treasury_debit_pending(db, buyer_slug, price):
        # 零行守卫(扫描到成交之间余额被别的段落动过):什么都没写,不 rollback
        # ——rollback 会 expire 整个 session(treasury_service 模块头军规 2)。
        return False

    cut = 0
    creator = None
    earned = 0
    if kind == "resident_work":
        cut = await treasury_service.skim_tax_pending(
            db, price, settings.town_tax_rate_sales, f"npc_sales_tax:{code}")
        earned = price - cut
        creator = (await db.execute(
            select(Resident).where(Resident.slug == offer["creator_slug"])
        )).scalar_one_or_none()
        if creator is None:  # 扫描后作者被清号:半笔 debit 必须就地回滚
            await db.rollback()
            return False
        if earned > 0:
            await coin_service.treasury_credit_pending(
                db, creator.slug, earned, reason=f"npc_work_sold:{code}")

    # 库存扣减:payload_json 没有 mutable 跟踪(app/models/shop.py:23),就地改会被
    # 静默丢弃 —— 整段镜像 shop_effects.py:330-348 的"拷贝→改→整体重赋值"。
    payload = dict(item.payload_json or {})
    stock = int(payload.get("stock", 1)) - 1
    payload["stock"] = max(0, stock)
    item.payload_json = payload
    if stock <= 0:
        item.active = False

    # 钱包缓存(prompt 读的那份)——事务内 SELECT 看得到自己尚未 commit 的改动。
    set_wallet_cache(db, buyer, await coin_service.treasury_balance(db, buyer_slug))
    creator_name = creator_id = creator_slug = None
    if creator is not None:
        creator_name, creator_id, creator_slug = creator.name, creator.id, creator.slug
        set_wallet_cache(db, creator,
                         await coin_service.treasury_balance(db, creator.slug))
    await db.commit()

    summary["bought"] += 1
    summary["spent"] += price
    summary["tax"] += cut
    await _narrate(buyer_id, buyer_slug, offer, price=price, earned=earned,
                   creator_name=creator_name, creator_id=creator_id,
                   creator_slug=creator_slug, db=db)
    return True


async def run_consumption_pass(db: AsyncSession, rng=None) -> dict:
    """夜间消费 pass(nightly #23)。返回 `{"bought", "spent", "tax"}` 摘要。

    每个自主居民每晚至多一笔、全镇至多 `npc_trade_max_buys_per_night` 笔;买方须
    在付款后仍保有 `npc_trade_reserve_sc` 保留金(兼作贫困线——买不起就是不买,
    赊账只在吃饭那条路上有)。`rng` 可注入,测试里是确定性的。
    """
    summary = {"bought": 0, "spent": 0, "tax": 0}
    if not (settings.npc_economy_enabled and settings.npc_trade_enabled):
        return summary

    rng = rng or random.Random()
    offers = await _offers(db)
    if not offers:
        return summary

    buyers = (await db.execute(
        select(Resident.id, Resident.slug)
        .where(Resident.is_autonomous)
        .order_by(Resident.slug)
    )).all()
    reserve = settings.npc_trade_reserve_sc
    cap = settings.npc_trade_max_buys_per_night

    for buyer_id, buyer_slug in buyers:
        if summary["bought"] >= cap:
            break
        if rng.random() >= settings.npc_trade_buy_prob:
            continue
        try:
            balance = await coin_service.treasury_balance(db, buyer_slug)
            picks = [o for o in offers
                     if o["creator_slug"] != buyer_slug       # 不买自己的作品
                     and balance > o["price"] + reserve]
            if not picks:
                continue
            best, best_score = None, 0.0
            for offer in picks:  # picks 按 code 稳定序,严格 > 保证并列取前者
                score = await _score(db, buyer_id, buyer_slug, offer)
                if best is None or score > best_score:
                    best, best_score = offer, score
            if await _buy(db, buyer_id, buyer_slug, best, summary):
                offers = await _offers(db)   # 库存/下架变了,重新扫一遍标的
        except Exception:
            # 写了半截就必须就地回滚:悬挂的 debit 会被后续买方的 commit 带落库。
            await db.rollback()
            logger.warning("npc_trade consumption failed for %s", buyer_slug,
                           exc_info=True)
            continue

    return summary
