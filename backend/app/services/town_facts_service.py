"""世界公共记忆 S2 —— 「小镇现况」公共事实读取层。

与 ``world_event_service`` 平行的第二条公共事实通道:那条讲**发生了什么**
(天气/节庆/新闻),这条讲**现在是什么样**(镇长是谁、谁管什么、税率多少、
镇上在议什么)。生产实测的缺陷就出在这里 —— 世界状态三处一致且正确,NPC 却
一个字都读不到,于是张口就是「赵启文在管事儿」。

本模块只负责**读**:不渲染(S3 的 ``format_town_facts``)、不注入(S5 的两个
接线点)、不写库。返回形状固定:

    {
      "mayor": {"slug": str, "name": str} | None,
      "duties": [{"slug": str, "name": str, "title": str}, ...],
      "policies": {key: value, ...},          # 仅 POLICY_WHITELIST
      "treasury_sc": int | None,              # None = 镇财政闸关,这个世界没有镇库
      "open_polls": [{"question": str, "options": [str, ...], "closes_at": str}, ...],
      "today": {"date": str, "weekday": int, "is_market_day": bool},
      "places": [str, ...],
    }

``civic_facts_enabled`` 关 → 返回 ``{}``(空字典是 falsy,下游一律「没有事实就
不多写一个字」)。

三条设计约束
------------
- **出网净化**:公投只出 question / options(仅 label) / closes_at。
  ``options_json`` 里并排躺着 ``npc_votes`` / ``_npc_voters`` / ``_proposer_slug``
  / ``effect``,那是内部计票状态,漏进 prompt 等于把票型和未生效的效果讲给 NPC 听。
- **有界 fail-open**:取数失败回落上一次快照,但只在
  ``civic_facts_max_stale_seconds`` 之内。宁可不注入,也不注入一个过期的镇长 ——
  错误的事实比没有事实伤害大。每次回落都打 ``CIVIC_FACTS_FAILOPEN`` 前缀的
  warning(agent-worker 不暴露 /metrics,日志 grep 是它那一侧唯一的可观测面)。
- **进程内快照**:api / agent-worker 双容器各持一份,``civic_facts_cache_ttl_seconds``
  内不重查。公共事实是天级变更,60s 的跨进程偏差无害,不为它引入 Redis。
  快照是**共享只读**的 —— 调用方拿到后不得原地改(要加字段就 ``{**facts, ...}``)。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import world_clock
from app.config import settings
from app.models.resident import Resident
from app.models.season import Poll
from app.observability import CIVIC_FACTS_FAILOPEN
from app.services import duty_service, election_service, treasury_service
from app.services.policy_service import PolicyService, catalog_default
from app.services.world_event_service import get_active_events_cached

logger = logging.getLogger(__name__)

#: 进 prompt 的政策白名单。政策表 15 条全倒进去会淹没上下文,而
#: ``constitutional_core`` 档(election_exists / exile_right / lab_approval_gate)
#: 对日常对话毫无意义 —— 只留居民真能体感的这 6 条。
POLICY_WHITELIST = (
    "tax_rate", "business_hours", "curfew_hours",
    "npc_default_wage_sc", "market_day_discount", "medical_subsidy_sc",
)

#: decide prompt 只拿这几类(K4:政策与镇库一律不进 decide —— 那条链路有
#: 「全文不得出现 tax / town_treasury / 镇财政 / 余额数字」的既有硬断言)。
DECIDE_FACT_KEYS = ("mayor", "today", "open_polls", "places")

#: ``ts`` 兼两职:TTL 计时起点,以及有界 fail-open 的陈旧度基准。只有**取数成功**
#: 才会推进它 —— 失败时不推,否则一次故障就能把旧快照的寿命无限续下去。
_cache: dict = {"ts": 0.0, "facts": {}}


class _SectionFailed(Exception):
    """某一类事实读取失败,带上 section 名做 fail-open 的 reason 标签。"""

    def __init__(self, section: str):
        super().__init__(section)
        self.section = section


def invalidate_town_facts_cache() -> None:
    """强制下一次调用重取(镇务写入侧在事实变更后调用)。

    只清 ``ts`` 不清 ``facts``:``facts`` 留着是给 fail-open 兜底用的,但因为
    ``ts`` 被清成 0,陈旧度判定必然不通过 —— 「已知作废的快照」不会被回落到。
    """
    _cache["ts"] = 0.0


def _reset_for_tests() -> None:  # pragma: no cover - test hook
    """K11:conftest 没有 autouse 重置,进程内快照会跨测试串味。"""
    _cache["ts"] = 0.0
    _cache["facts"] = {}


# ── 各类事实的读取 ──────────────────────────────────────────────────────

async def _read_mayor(db: AsyncSession) -> dict | None:
    """现任镇长。权威源只有 ``election_service.current_mayor``(它内部按
    ``polis_office_enabled`` 决定 offices / system_config 谁优先),绝不裸读
    offices —— 那张表运行时只有 mayor 一行被写。返回 slug(K12),名字再解析。"""
    slug = await election_service.current_mayor(db)
    if not slug:
        return None
    name = (await db.execute(
        select(Resident.name).where(Resident.slug == slug)
    )).scalar_one_or_none()
    # 居民行被删而 current_mayor 还留着 slug:退回 slug 本身,别让整段 fail-open。
    return {"slug": slug, "name": name or slug}


async def _read_duties(db: AsyncSession) -> list[dict]:
    """在任营生。收录口径 ``is_autonomous``(K13 的 SQL expression 可直接进
    WHERE;K15:含 UGC resident,只排除 player)。

    这是「按人读营生」方向,唯一入口是 ``duty_service.get_duty(resident)``;
    「按 key 反查持有人」是另一个方向,走 ``duty_service.find_duty_resident``。
    按 slug 排序:prompt 快照必须稳定,否则每次重取都可能换一个顺序。
    """
    residents = (await db.execute(
        select(Resident)
        .where(Resident.is_autonomous, Resident.meta_json.isnot(None))
        .order_by(Resident.slug)
    )).scalars().all()
    out: list[dict] = []
    for r in residents:
        title = duty_service.get_duty(r).get("title")
        if title:
            out.append({"slug": r.slug, "name": r.name, "title": title})
    return out


async def _read_policies(db: AsyncSession) -> dict:
    """生效政策(仅白名单)。

    M1:逐键 ``PolicyService.get()`` 而不是 ``list_all()`` —— 后者在
    ``polis_policy_enabled=False`` 时直接返 ``[]``,而 ``get()`` 会回落
    ConfigService,本地/新环境也读得到值。默认值取 ``catalog_default(key)``
    (政策目录原文,settings-backed 的那两键在调用时才解析),不要
    ``getattr(settings, key, None)``:``curfew_hours`` / ``business_hours`` /
    ``medical_subsidy_sc`` 在 Settings 里压根没有同名字段。
    """
    svc = PolicyService(db)
    return {key: await svc.get(key, default=catalog_default(key))
            for key in POLICY_WHITELIST}


async def _read_treasury(db: AsyncSession) -> int | None:
    """镇库余额。闸关返回 ``None`` 而不是 0 —— 镇财政没跑的世界里根本没有「镇库」
    这个概念,注入「余额 0 枚硬币」是编事实。None 与 0 在渲染层语义不同。"""
    if not settings.town_treasury_enabled:
        return None
    return await treasury_service.balance(db)


async def _read_open_polls(db: AsyncSession) -> list[dict]:
    """进行中的公投。只出 question / options(仅 label) / closes_at —— 见模块
    docstring 的出网净化条款。按截止时间排序,最先截止的排前面。"""
    polls = (await db.execute(
        select(Poll).where(Poll.status == "open").order_by(Poll.closes_at)
    )).scalars().all()
    return [{
        "question": p.question,
        "options": [o["label"] for o in (p.options_json or [])
                    if isinstance(o, dict) and o.get("label")],
        "closes_at": p.closes_at.isoformat() if p.closes_at else None,
    } for p in polls]


async def _read_today(db: AsyncSession) -> dict:
    """今天:世界日期 / 星期 / 是否集市日。

    M2:``is_market_day`` 不能自己按 weekday 算 —— ``market_day_weekday`` 本身是
    可公投改的政策键,自己算必然与世界漂移。唯一判据是活跃世界事件的 payload
    (与 ``shop_service`` 的折扣判定同一条)。``get_active_events_cached`` 自带
    60s 缓存,chat 那侧本来就要取,零额外查询。
    """
    events = await get_active_events_cached(db)
    return {
        "date": world_clock.world_date_key(),
        "weekday": world_clock.world_weekday(),
        "is_market_day": any((e.get("payload_json") or {}).get("market_day")
                             for e in events),
    }


async def _read_places(db: AsyncSession) -> list[str]:
    """小镇有哪些地方。只列公共设施:私宅/公寓是住址不是地标,几十个名字还会把
    prompt 预算吃光。``db`` 参数是为了与其它 section 同签名(本段纯内存)。"""
    from app.agent.map_data import LOCATIONS

    return [loc["name"] for loc in LOCATIONS.values()
            if loc.get("type") == "public" and loc.get("name")]


#: (section 名, 读取函数) —— 顺序即返回字典的键序,也是 fail-open 的 reason 取值域。
_SECTIONS: tuple[tuple[str, Callable[[AsyncSession], Awaitable]], ...] = (
    ("mayor", _read_mayor),
    ("duties", _read_duties),
    ("policies", _read_policies),
    ("treasury_sc", _read_treasury),
    ("open_polls", _read_open_polls),
    ("today", _read_today),
    ("places", _read_places),
)


# ── 公开 API ────────────────────────────────────────────────────────────

async def _collect_public_facts(db: AsyncSession) -> dict:
    """全量取一遍。任何一段失败都整体失败 —— 半截事实(有镇长没政策)比没有事实
    更难排查,而 fail-open 那层本来就会回落到一份完整的旧快照。"""
    facts: dict = {}
    for section, reader in _SECTIONS:
        try:
            facts[section] = await reader(db)
        except Exception as exc:
            raise _SectionFailed(section) from exc
    return facts


def _fail_open(now: float, reason: str) -> dict:
    """有界 fail-open:陈旧上限内回落旧快照,超了就交白卷。"""
    max_stale = settings.civic_facts_max_stale_seconds
    fresh_enough = (_cache["ts"] > 0.0 and bool(_cache["facts"])
                    and now - _cache["ts"] <= max_stale)
    try:
        CIVIC_FACTS_FAILOPEN.labels(reason=reason).inc()
    except Exception:  # pragma: no cover - 指标永远不该反过来打断调用方
        logger.debug("CIVIC_FACTS_FAILOPEN counter failed", exc_info=True)
    # 固定前缀便于 grep:agent-worker 侧没有 /metrics,日志是它唯一的可观测面。
    logger.warning("CIVIC_FACTS_FAILOPEN reason=%s served=%s", reason,
                   "stale" if fresh_enough else "empty", exc_info=True)
    return _cache["facts"] if fresh_enough else {}


async def get_town_facts_cached(db: AsyncSession) -> dict:
    """公共事实快照(进程内缓存的唯一一层)。闸关 → ``{}``。

    per-resident 的「自身事实」绝不能进这里 —— 一个 uvicorn worker 内所有会话
    共用同一份快照,放进来就是串人。那部分由 S4 的 ``build_town_facts`` 现算。
    """
    if not settings.civic_facts_enabled:
        return {}

    now = time.monotonic()
    if _cache["ts"] > 0.0 and now - _cache["ts"] < settings.civic_facts_cache_ttl_seconds:
        return _cache["facts"]

    try:
        facts = await _collect_public_facts(db)
    except _SectionFailed as exc:
        return _fail_open(now, exc.section)
    except Exception:  # pragma: no cover - 兜底,_collect 之外的意外
        return _fail_open(now, "unknown")

    _cache["ts"] = now
    _cache["facts"] = facts
    return facts
