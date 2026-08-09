"""World event service (S1): active-event cache + cron flip logic.

The active-event list is read on every agent tick and every player-dialogue
prompt build, so it is cached process-locally for 60s to avoid hammering the DB.
The cron invalidates the cache when it flips events so a fresh snapshot is picked
up within one tick of a transition.
"""

import time
import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.world_event import WorldEvent

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60.0
_cache: dict = {"ts": 0.0, "events": []}


def _to_dict(e: WorldEvent) -> dict:
    return {
        "id": e.id,
        "type": e.type,
        "title": e.title,
        "description": e.description,
        "payload_json": e.payload_json or {},
        "starts_at": e.starts_at.isoformat() if e.starts_at else None,
        "ends_at": e.ends_at.isoformat() if e.ends_at else None,
    }


def invalidate_active_cache() -> None:
    """Force the next get_active_events_cached call to re-query."""
    _cache["ts"] = 0.0


async def get_active_events_cached(db: AsyncSession) -> list[dict]:
    """Return active events as dicts, refreshing at most once per CACHE_TTL."""
    now = time.monotonic()
    if now - _cache["ts"] < CACHE_TTL_SECONDS and _cache["ts"] > 0.0:
        return _cache["events"]
    try:
        result = await db.execute(select(WorldEvent).where(WorldEvent.is_active.is_(True)))
        events = [_to_dict(e) for e in result.scalars().all()]
    except Exception:
        # Fail open: never let a query error break perceive/dialogue.
        logger.warning("world_event active query failed", exc_info=True)
        return _cache["events"]

    # C3: the active season's world-view patch rides along as a synthetic event
    # so it lands in every resident prompt without touching each consumer.
    try:
        wv = await _active_season_worldview(db)
        if wv:
            events = events + [wv]
    except Exception:
        logger.warning("season world-view injection failed", exc_info=True)
    _cache["ts"] = now
    _cache["events"] = events
    return events


async def _active_season_worldview(db: AsyncSession) -> dict | None:
    """C3: the active season's ``payload_json['world_view']`` as an event dict."""
    from app.models.season import Season

    season = (await db.execute(
        select(Season).where(Season.status == "active").order_by(Season.starts_at.desc())
    )).scalars().first()
    if season is None:
        return None
    wv = (season.payload_json or {}).get("world_view")
    if not wv:
        return None
    return {"id": f"season:{season.id}", "type": "season", "title": season.title,
            "description": wv, "payload_json": {}, "starts_at": None, "ends_at": None}


async def flip_active_events(db: AsyncSession) -> list[tuple[dict, str]]:
    """Turn events on/off based on the current time.

    Returns a list of (event_dict, phase) where phase is "start" or "end" for
    each event whose is_active flag changed this pass.
    """
    now = datetime.now(UTC)
    changes: list[tuple[dict, str]] = []

    result = await db.execute(select(WorldEvent))
    for e in result.scalars().all():
        # Compare naive/aware safely: normalize stored datetimes to aware UTC.
        starts = e.starts_at
        ends = e.ends_at
        if starts is not None and starts.tzinfo is None:
            starts = starts.replace(tzinfo=UTC)
        if ends is not None and ends.tzinfo is None:
            ends = ends.replace(tzinfo=UTC)

        should_be_active = (starts is not None and starts <= now) and (ends is not None and ends > now)
        if should_be_active and not e.is_active:
            e.is_active = True
            changes.append((_to_dict(e), "start"))
        elif not should_be_active and e.is_active:
            e.is_active = False
            changes.append((_to_dict(e), "end"))

    if changes:
        await db.commit()
        invalidate_active_cache()
    return changes


#: 琐事档的事件类型。天气是 96% 的量(生产 1311 条 world_event 记忆里 1253 条),
#: 而候选池只有 30 个坑 —— 把它抬进池子等于拿「今天多云」把个人记忆挤光,那比
#: 「写了等于没写」更糟。琐事档**刻意**不参与候选池的竞争。
TRIVIAL_EVENT_TYPES = ("weather",)


def _is_trivial_event(event: dict) -> bool:
    """琐事档判据:天气,或**集市日**那一档节庆。

    集市日归琐事是本批唯一需要论证的分类判断:

    - 它**每周复现**(``event_templates`` 按 ``market_day_weekday`` 生成),进实质档
      意味着每周给全镇各加一条顶档记忆,长期只会自我复制;
    - NPC 已经**从事实层**知道今天是不是集市日(``town_facts`` 的
      ``today.is_market_day``,本身就是读活跃事件的 payload),不需要再靠记忆检索;
    - 儿童节 / 公开课 / news / script 都是一次性的叙事事件,那才是「记得那天发生过
      什么」该占坑的东西。

    ``market_day`` 判据与 ``shop_service._market_discount`` / ``event_cron`` 的商队
    钩子同源(都读事件 payload),别在这里另起一份口径。
    """
    if event.get("type") in TRIVIAL_EVENT_TYPES:
        return True
    return (event.get("type") == "festival"
            and bool((event.get("payload_json") or {}).get("market_day")))


async def _write_substantive(db: AsyncSession, informed: dict[str, float],
                             content: str, meta: dict) -> int:
    """实质档:逐人走 ``MemoryService.add_memory``。返回写入条数。

    为什么必须绕开直写:``add_memory`` 是唯一会过 ``_normalize_importance`` 的入口
    (``memory/service.py:77-80``)。直写落的 importance 是**绝对值**,而候选池
    ``_fetch_event_candidates`` 按 ``importance DESC`` 静态截前 30
    (``memory/service.py:308``),生产实测每位居民第 30 名都在 0.95-1.0 —— 0.5/0.6
    一条都进不去。归一后的落库值是**分位数**,与 civic 结果档同一档位。

    raw 对 geo 与随机样本**取同一个值**(``realism_event_memory_importance``),不沿用
    直写那两档 0.6/0.5:那两个数是「谁知道」的副产品,拿它当「多重要」等于说同一件事
    在旁观者脑子里天生轻一档 —— 重要性是**事件**的属性,知情路径是**收件人**的属性。
    梯度已经在上面把收件人筛过了,这里只管档位。

    N+1:每人一次归一化查询(窗口按 ``resident_id`` 过滤,天然合并不成一条)。14 人
    = 14 次轻查询,成本随人口线性增长。不做批量是因为实质档按定义稀疏(生产
    58/1311 = 4% 的量),而批量化要么把窗口改成全镇共用(分位数就失去 per-resident
    的意义),要么手写一条 window function —— 都不抵这点收益。

    事务:``add_memory`` 自带 ``commit()``(``memory/service.py:95``),所以这里是逐人
    落地。调用点 ``tasks/event_cron.py:41`` 的同一个 session 后面还要给 C4 商队 /
    C3 / E3 用,但逐人 commit **不会**劈开它们的事务边界 —— 上游 ``flip_active_events``
    在本函数之前已经 commit 过,本函数改前也是以 commit 收尾,前后都不存在跨步骤的
    未决写。反而更安全:改前中途抛异常会把半截 pending 的 Memory 留在 session 里,
    由下一步(C4 商队)的 commit 顺手带进它自己的事务。

    fail-open + rollback:半截失败只记 warning 并返回**已写条数**。rollback 是这条
    路新增的必需品 —— 调用点对本函数的 except 分支**没有** rollback
    (``event_cron.py:40-43``),而 commit 点从 1 个变成了 N 个,不收干净的话
    ``PendingRollbackError`` 会顺着传染并被误算到 C4/C3/E3 头上(``event_cron.py:60-62``
    记着同一个坑)。
    """
    from app.config import settings
    from app.memory.service import MemoryService

    svc = MemoryService(db)
    written = 0
    try:
        for rid in informed:
            await svc.add_memory(rid, "event", content,
                                 settings.realism_event_memory_importance,
                                 "world_event", metadata_json=dict(meta))
            written += 1
    except Exception:
        logger.warning("WORLD_EVENT_MEMORY_FAILED event_id=%s written=%d",
                       meta.get("event_id"), written, exc_info=True)
        try:
            await db.rollback()
        except Exception:
            logger.warning("world_event memory rollback itself failed", exc_info=True)
    return written


def _geo_relevant_residents(event: dict, rows) -> set[str]:
    """Resident ids currently within ``realism_info_geo_radius`` tiles of the
    event's location. The event's location comes from ``payload_json.location_id``
    (Manhattan distance to the location center). Empty when the event names no
    location. NOTE: location_visits is player-scoped, so the plan's "7-day
    visitors" signal is not available for residents — current position within
    radius is the resident geo-relevance signal (see PROGRESS P2-5 deviation)."""
    from app.config import settings
    from app.agent.map_data import get_location_by_id
    loc_id = (event.get("payload_json") or {}).get("location_id")
    if not loc_id:
        return set()
    loc = get_location_by_id(loc_id)
    if not loc:
        return set()
    center = loc.get("center")
    if not center:
        b = loc.get("bounds")
        if b:
            center = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    if not center:
        return set()
    radius = settings.realism_info_geo_radius
    cx, cy = center
    return {rid for rid, tx, ty in rows if abs((tx or 0) - cx) + abs((ty or 0) - cy) <= radius}


async def write_collective_memories(db: AsyncSession, event: dict, rng=None) -> int:
    """On a world event start, write first-hand event memories (batch, no LLM).

    Pre-P2 / gate off: every active (non-sleeping) resident gets one — the old
    all-knowing broadcast. With ``REALISM_INFO_GRADIENT_ENABLED`` on, non-weather
    events instead inform only (a) residents geographically near the event
    (importance 0.6) and (b) a random ``sample_frac`` "well-informed" sample of
    the rest (importance 0.5); everyone else learns it second-hand via gossip
    (§8.1). Weather stays all-broadcast ("抬头可见"). First-hand memories carry
    ``event_id`` in metadata so the diffusion probe + gossip can follow them.
    Returns the number of first-hand memories written.

    ``REALISM_EVENT_MEMORY_TIERED`` 再叠一层**分档**,与上面那条梯度正交:梯度决定
    「谁知道」(收件人集合,本批不动),分档决定「多重要」(琐事直写 / 实质走
    ``add_memory`` 的分位归一,见 ``_is_trivial_event`` 与 ``_write_substantive``)。
    闸关 = 全部直写,与改前逐字节一致。"""
    import random as _random
    from app.config import settings
    from app.models.resident import Resident
    from app.models.memory import Memory

    rng = rng or _random
    rows = (await db.execute(
        select(Resident.id, Resident.tile_x, Resident.tile_y).where(Resident.status != "sleeping")
    )).all()
    content = (event.get("description") or event.get("title") or "")[:200]
    if not content or not rows:
        return 0
    event_id = event.get("id")
    is_weather = event.get("type") == "weather"

    def _meta():
        m = {"first_hand": True}
        if event_id:
            m["event_id"] = event_id
        return m

    if not settings.realism_info_gradient_enabled or is_weather:
        # All-knowing broadcast (pre-P2 path; weather keeps it — sky is visible).
        # dict 而不是直接写循环:下面的分档要在**同一份收件人集合**上分岔,两条
        # 梯度分支各写一遍写入代码就是两份口径。rid 是主键,不会有重复键把人吞掉。
        informed: dict[str, float] = {rid: 0.5 for rid, _, _ in rows}
    else:
        # Information gradient: geo-related + a random well-informed sample.
        geo_ids = _geo_relevant_residents(event, rows)
        informed = {rid: settings.realism_info_geo_importance for rid in geo_ids}
        others = [rid for rid, _, _ in rows if rid not in geo_ids]
        k = round(settings.realism_info_sample_frac * len(others))
        for rid in rng.sample(others, min(k, len(others))):
            informed.setdefault(rid, settings.realism_info_sample_importance)

    # 分档:琐事(天气 / 集市日)照旧直写,实质事件改走归一化。闸关 = 恒走直写。
    if settings.realism_event_memory_tiered and not _is_trivial_event(event):
        return await _write_substantive(db, informed, content, _meta())

    for rid, imp in informed.items():
        db.add(Memory(resident_id=rid, type="event", content=content,
                      importance=imp, source="world_event", metadata_json=_meta()))
    await db.commit()
    return len(informed)
