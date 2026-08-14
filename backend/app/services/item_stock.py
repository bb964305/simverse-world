"""M-A 加固 — items 库存的唯一原子扣减口。

**为什么要有这个模块。** 扣减原本在三处各写一遍(``shop_effects``、
``npc_trade_service``、``caravan_service``),形态都是 ORM 读-改-写:
``payload = dict(item.payload_json); stock -= qty; 整体重赋值``。cron 进程(商队
到访 / NPC 夜间消费)与 API 玩家购买撞上同一行 items 时,两边都读到 stock=1、都
写回 0 —— 一件货卖了两次。钱是守恒的(各人付各人的),但库存不是,而且作者被付了
两份货款。

**根治。** stock 从 ``payload_json`` 抬成真列(迁移 056)之后,判据可以写进
WHERE:``UPDATE items SET stock = stock - qty WHERE code = :code AND active AND
stock >= qty``。判据与写入在同一条语句里,互斥交给数据库,零行就是"没抢到"。

**两条路径。** ``ITEM_STOCK_GUARD_ENABLED`` 关 = 逐字节旧路径(且**永不返回
None**,旧行为里没有"抢不到"这回事),所以迁移可以先暗上、开闸是另一次变更
(红线:迁移与行为变更不同车)。唯一例外是 durable 商队生命周期已经开启时：它
自身必须用列 CAS，玩家/NPC 若仍走 payload 就会形成两个互相覆盖的库存账本。因此
``CARAVAN_LIFECYCLE_ENABLED`` 同时作为安全背板，强制所有消费者走列真相；部署仍应
先显式打开库存守卫，安全不能只靠运维顺序。列路径里
``payload_json['stock']`` 仍被同步更新——它退化成镜像，但回滚开关也不会丢账。

**事务纪律。** flush-owned:不 commit(调用方拥有事务);守卫零行时**不
rollback** —— 什么都没写,而 rollback 会 expire 调用方 session 里的所有 ORM 对象
(``treasury_service`` 模块头军规 2:asyncio 下一次惰性取属性就是 MissingGreenlet)。
调用方拿到 ``None`` 时:此前什么都没写就直接返回;已经动过钱(付款/扣款)就地
``rollback`` 再退出。
"""
from __future__ import annotations

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.shop import Item


def _legacy_take(item: Item, qty: int) -> int:
    """闸关路径:与旧的三处 payload 读-改-写逐行等价。

    ``payload_json`` 没有 mutable 跟踪(app/models/shop.py),就地改会被静默丢弃,
    所以必须"拷贝→改→整体重赋值"。永不返回 None,也不碰 ``items.stock`` 列 ——
    暗上期间那一列必须一个字节都不动。
    """
    payload = dict(item.payload_json or {})
    stock = int(payload.get("stock", 1)) - qty
    payload["stock"] = max(0, stock)
    item.payload_json = payload
    if stock <= 0:
        item.active = False
    return max(0, stock)


async def take_authoritative_stock(
    db: AsyncSession, item: Item, qty: int = 1,
) -> int | None:
    """始终从 ``items.stock`` 原子扣减；payload 只作为兼容镜像。

    lifecycle settlement 与普通玩家/NPC路径共用这一实现，避免两个列 CAS
    版本在 NULL 自愈、active 切换或镜像写回上再次漂移。调用方拥有事务。
    """
    if qty < 1:
        return None

    row = (await db.execute(
        select(Item.stock, Item.payload_json).where(Item.code == item.code)
    )).first()
    if row is None:
        return None
    if row.stock is None:
        # 暗上窗口(迁移已落库、闸还没翻)里由旧镜像挂上架的行:列还是 NULL。
        # 守卫 ``stock IS NULL`` 让两个进程同时自愈也只落一次。
        await db.execute(
            update(Item)
            .where(Item.code == item.code, Item.stock.is_(None))
            .values(stock=int((row.payload_json or {}).get("stock", 1)))
            .execution_options(synchronize_session=False)
        )

    result = await db.execute(
        update(Item)
        .where(Item.code == item.code, Item.active.is_(True), Item.stock >= qty)
        .values(
            stock=Item.stock - qty,
            active=case((Item.stock - qty <= 0, False), else_=True),
        )
        .execution_options(synchronize_session=False)
    )
    if (result.rowcount or 0) == 0:
        return None

    # synchronize_session=False:内存里的 item 是旧的,剩余量只能重读(事务内
    # SELECT 看得到自己尚未 commit 的改动)。
    remaining = (await db.execute(
        select(Item.stock).where(Item.code == item.code))).scalar_one()
    payload = dict(item.payload_json or {})
    payload["stock"] = remaining
    item.payload_json = payload
    if remaining <= 0:
        item.active = False
    await db.flush()
    return remaining


async def take_stock(db: AsyncSession, item: Item, qty: int = 1) -> int | None:
    """扣 ``qty`` 件库存。返回扣完剩余；守卫零行返回 ``None``。

    返回 ``None`` 的情形：售罄(``stock < qty``)、已下架(``active`` 为假)、行没
    了、``qty < 1``。durable 商队开启时，即使独立库存闸因错误配置仍为 false，
    也必须走 authoritative 列路径；否则 lifecycle 会把玩家刚写的 payload 扣减
    用旧列值覆盖，重新放出已售库存。
    """
    if qty < 1:
        return None
    if not (
        settings.item_stock_guard_enabled
        or settings.caravan_lifecycle_enabled
    ):
        return _legacy_take(item, qty)
    return await take_authoritative_stock(db, item, qty)
