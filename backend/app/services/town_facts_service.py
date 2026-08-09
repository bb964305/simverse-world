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

``build_town_facts(db, resident)`` 在这份公共快照之上再挂一段 per-resident 的
``"self"``(营生 + 议题立场),排在键序最后。那一段**绝不进缓存**,理由见下面第
三条。

四条设计约束
------------
- **出网净化**:公投只出 question / options(仅 label) / closes_at。
  ``options_json`` 里并排躺着 ``npc_votes`` / ``_npc_voters`` / ``_proposer_slug``
  / ``effect``,那是内部计票状态,漏进 prompt 等于把票型和未生效的效果讲给 NPC 听。
  自由文本还要多过两道:原始政策键折成中文标签(``scrub_policy_keys``),以及下面
  这一条的量纲上限。
- **量纲有上限**:每一类事实的条数与单条长度都卡死(见「量纲上限」那组常量)。
  公投标题/选项、地点名、UGC 居民的名字与头衔都是玩家写的自由文本,而这一层的
  输出直接进 prompt —— 没有上限就等于把 prompt 预算的写权限开放给了任何一个持
  Bearer token 的人。
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
from app.services.opinion_service import OpinionService
from app.services.policy_labels import scrub_policy_keys
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

#: 自身事实里最多带几条议题立场。生产一个居民同时挂着的议题不多,3 条足够表达
#: 「他最近在意什么」,再多就是拿 prompt 预算换边际信息。
SELF_STANCE_LIMIT = 3

#: ``issue_key`` 是自由文本 ``String(300)``,原样折进去一条就能吃掉四分之一的
#: 段落预算(S11 的硬上限是 1200 字符)。
_ISSUE_MAX_CHARS = 30

# ── 量纲上限:每一类事实的条数与单条长度 ───────────────────────────────────
#
# 为什么必须在**读侧**设:这一层读出来的东西直接进每位 NPC 的 system prompt 与
# decide prompt,而其中三类的内容是**玩家自由文本**——``POST /polls/propose`` 只
# 要一个 Bearer token,topic 与 options[].label 就落进 ``polls`` 表;地点名来自
# 公投 effect 的 data;UGC 居民的 name 与 duty title 是玩家造的。这些地方一处都
# 没有天然背压:条数无上限 = 谁都能把整段 prompt 预算买断,单条无上限 = 一条就够。
#
# S11 的「段落 < 1200 字符」量的是固定合成输入,那是**我们喂进去的**;这几个常量
# 才是运行时保证。数字按 1200 的分账取:营生 ~550 / 公投 ~560 / 地点 ~115,其余
# (镇长/政策/镇库/今天/自身事实)是有界的固定形状,合计留出余量。
# 由 tests/test_civic_prompt_budget.py::test_ugc_flood_cannot_blow_the_char_budget
# 实测兜住,改这里的任何一个数都会被那条测试算总账。

#: 最多带几张进行中公投。按 ``closes_at`` 取最近截止的几张——马上要投的那几张才
#: 是「镇上正在议的事」。
OPEN_POLLS_LIMIT = 5
POLL_QUESTION_MAX_CHARS = 40
#: 单张公投最多列几个选项 / 单个选项多长。选项是给 NPC 一个「在议什么」的概念,
#: 不是选票——列全没有额外价值,列爆有。
POLL_OPTIONS_LIMIT = 4
POLL_OPTION_MAX_CHARS = 10

#: 在任营生最多列几人。今天是 14 人(11 preset + 3 UGC),UGC 只增不减。
DUTIES_LIMIT = 20
DUTY_NAME_MAX_CHARS = 10
DUTY_TITLE_MAX_CHARS = 14

#: 公共去处最多列几处。今天是 10 处(8 静态 + 公投建的邮局/剧院),而公投能接着建。
PLACES_LIMIT = 12
PLACE_MAX_CHARS = 8

#: ``stance ∈ [-1, 1]`` 的定性分档。**数值本身永不进 prompt** —— spec §2 的非目标
#: 与 civic_service.py:644 的既有设计约束(探针数值不给 NPC 看)是同一条。
_STANCE_SUPPORT = 0.2
_STANCE_OPPOSE = -0.2

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


def _clip(text: str, limit: int) -> str:
    """出网前截断到 ``limit`` 个字符(含省略号,所以结果长度恒 ≤ limit)。

    唯一的截断实现:议题键、公投标题与选项、营生名与头衔、地点名共用它。各处的
    上限不同,截断姿势必须相同 —— 否则「≤ limit」这条在某一处会悄悄变成 limit+1。
    """
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


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

    ``DUTIES_LIMIT`` / 名字与头衔的截断:居民数今天有界,但 UGC 居民是玩家造的,
    ``name`` 顶得到 ``String(100)``,``title`` 在 ``meta_json`` 里压根没有列宽。
    上限卡在**输出条数**而不是 SQL ``LIMIT``:后者会让一串没有 title 的居民把有
    title 的人挤出名单(先截断再过滤 = 截断决定了谁被看见)。
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
            out.append({"slug": r.slug,
                        "name": _clip(r.name, DUTY_NAME_MAX_CHARS),
                        "title": _clip(title, DUTY_TITLE_MAX_CHARS)})
            if len(out) >= DUTIES_LIMIT:
                break
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
    docstring 的出网净化条款。按截止时间排序,最先截止的排前面。

    ``question`` 出网前折一道原始政策键(``scrub_policy_keys``)。
    ``policy_service._open_amend_poll`` 造的标题里嵌着英文键
    (``将「tax_rate」调整为 0.05``,生产 08-11 截止的那张就是),而这条链路经
    ``DECIDE_FACT_KEYS`` 直通 decide prompt,撞 K4 的 ``"tax" not in blob.lower()``。
    写侧同一道折叠治新数据,**已落库的标题只有这里够得着**。

    ``question`` 与每个选项 label 都是玩家自由文本(``POST /polls/propose`` 只要
    一个 Bearer token),所以条数与单条长度都要卡:只取最近截止的
    ``OPEN_POLLS_LIMIT`` 张(马上要投的那几张才是「镇上正在议的事」),标题截到
    ``POLL_QUESTION_MAX_CHARS``,选项截条数也截长度。
    """
    polls = (await db.execute(
        select(Poll).where(Poll.status == "open")
        .order_by(Poll.closes_at).limit(OPEN_POLLS_LIMIT)
    )).scalars().all()
    return [{
        "question": _clip(scrub_policy_keys(p.question), POLL_QUESTION_MAX_CHARS),
        # 先过滤再截条数:反过来会让开头几个没有 label 的内部条目把真选项挤掉。
        "options": [_clip(o["label"], POLL_OPTION_MAX_CHARS)
                    for o in (p.options_json or [])
                    if isinstance(o, dict) and o.get("label")][:POLL_OPTIONS_LIMIT],
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
    """小镇有哪些地方 = 静态公共设施 + 公投/Lab 建出来的 active 动态地点。

    只列公共设施:私宅/公寓是住址不是地标,几十个名字还会把 prompt 预算吃光。
    动态地点沿用同一条口径(``data_json["type"] == "public"``)。

    为什么还要再查一次库 —— ``map_data.load_dynamic_locations`` 只在进程启动和
    ``sv:world:reload`` 信号时把 active 行并进 LOCATIONS:那份内存快照可能比世界
    晚(新落成的楼要等下一次 reload)也可能比世界早(停用的行还留在里面)。库里的
    ``active`` 才是当下的事实。已经并过的楼会被两边各数一次,所以按名字去重
    (``dict.fromkeys`` 保序,静态在前、动态按 slug 追加,快照顺序稳定)。

    ``PLACES_LIMIT`` / ``PLACE_MAX_CHARS``:动态地点的名字来自公投 effect 的
    ``data``,是自由文本;而公投能一直建楼,条数只增不减。**先截断再去重**——
    去重后再截断会让两个只有尾巴不同的长名字在 prompt 里并排出现同一个词。
    静态设施在前,所以被条数上限挤掉的总是新加的动态地点。
    """
    from app.agent.map_data import LOCATIONS
    from app.models.dynamic_location import DynamicLocation

    names = [loc["name"] for loc in LOCATIONS.values()
             if loc.get("type") == "public" and loc.get("name")]
    rows = (await db.execute(
        select(DynamicLocation.data_json)
        .where(DynamicLocation.active.is_(True))
        .order_by(DynamicLocation.slug)
    )).scalars().all()
    names += [data["name"] for data in (r or {} for r in rows)
              if data.get("type") == "public" and data.get("name")]
    clipped = (_clip(name, PLACE_MAX_CHARS) for name in names)
    return list(dict.fromkeys(clipped))[:PLACES_LIMIT]


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


# ── 自身事实(per-resident,永不进共享快照) ───────────────────────────────

def _stance_label(stance: float) -> str:
    """立场数值 → 说得出口的态度。阈值两侧之间一律读作「中立」:0.05 与 -0.05 的
    差别对一句对话毫无意义,却会让 NPC 每次微漂移都改口。"""
    if stance > _STANCE_SUPPORT:
        return "支持"
    if stance < _STANCE_OPPOSE:
        return "反对"
    return "中立"


async def _collect_self(db: AsyncSession, resident) -> dict:
    """「关于你自己的事实」:营生 + 最近的议题立场。

    M5:``duty_title`` 只是标签,真正可对话的事实在 ``duty_hint``。取
    ``get_duty()`` 里的 ``prompt_hint`` **原文** —— 不走 ``duty_service.prompt_hint()``,
    它带 ``\\n`` 前缀和 decide 口吻,那是给决策 prompt 拼的。

    M6:立场读侧本身没有闸门,这里替它定义语义 —— ``polis_opinion_enabled`` 关
    的世界里舆论动力学压根没在跑,表里剩的是上一纪元的残值,不能拿去当「他现在
    的态度」。闸关顺带省掉这一次查询。
    """
    duty = duty_service.get_duty(resident)
    stances: list[dict] = []
    if settings.polis_opinion_enabled:
        rows = await OpinionService(db).list_stances(
            resident.slug, limit=SELF_STANCE_LIMIT)
        stances = [{"issue": _clip(key, _ISSUE_MAX_CHARS),
                    "label": _stance_label(stance)}
                   for key, stance in rows]
    return {
        "duty_title": duty.get("title"),
        "duty_hint": duty.get("prompt_hint"),
        "stances": stances,
    }


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


def _note_failopen(reason: str, served: str) -> None:
    """记一笔 fail-open。``reason`` 的取值域 = 各 section 名 + ``self`` + ``unknown``。"""
    try:
        CIVIC_FACTS_FAILOPEN.labels(reason=reason).inc()
    except Exception:  # pragma: no cover - 指标永远不该反过来打断调用方
        logger.debug("CIVIC_FACTS_FAILOPEN counter failed", exc_info=True)
    # 固定前缀便于 grep:agent-worker 侧没有 /metrics,日志是它唯一的可观测面。
    logger.warning("CIVIC_FACTS_FAILOPEN reason=%s served=%s", reason, served,
                   exc_info=True)


def _fail_open(now: float, reason: str) -> dict:
    """有界 fail-open:陈旧上限内回落旧快照,超了就交白卷。"""
    max_stale = settings.civic_facts_max_stale_seconds
    fresh_enough = (_cache["ts"] > 0.0 and bool(_cache["facts"])
                    and now - _cache["ts"] <= max_stale)
    _note_failopen(reason, "stale" if fresh_enough else "empty")
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


async def build_town_facts(db: AsyncSession, resident=None) -> dict:
    """一位居民眼里的「小镇现况」= 公共快照 + 他自己那一段。**本层不进缓存**。

    B3:两层 API 是硬要求,不是洁癖。公共快照是模块级的,一个 uvicorn worker 内
    所有会话共用同一份 —— 把 per-resident 的 ``self`` 原地塞进去,下一个来聊天的
    人就会拿着别人的营生和立场开口。所以这里只 ``{**public, ...}`` 出一份新字典,
    共享那份始终只有公共的 7 类。

    ``resident=None``(NPC↔NPC、或调用方压根不关心自身事实)与闸关(``public``
    为空)都直接返回公共部分:闸门关着还硬贴一段自身事实,等于绕过闸门。
    """
    public = await get_town_facts_cached(db)
    if resident is None or not public:
        return public
    try:
        return {**public, "self": await _collect_self(db, resident)}
    except Exception:
        # 自身事实取不到不该连累公共事实:少一段,总好过整段哑掉。
        _note_failopen("self", "public")
        return public
