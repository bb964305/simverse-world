import logging
import math
from datetime import datetime, timedelta, UTC
from itertools import zip_longest
from sqlalchemy import select, func, delete, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.memory import Memory
from app.llm.client import chat as llm_chat
from app.llm.metering import Meter
from app.llm.json_extract import extract_json_object
from app.memory.embedding import generate_embedding
from app.memory.prompts import (
    EXTRACT_EVENTS_SYSTEM,
    EXTRACT_EVENTS_USER,
    UPDATE_RELATIONSHIP_SYSTEM,
    UPDATE_RELATIONSHIP_USER,
    REFLECT_SYSTEM,
    REFLECT_USER,
    CHAT_WRAPUP_SYSTEM,
    CHAT_WRAPUP_USER,
    sbti_coloring_block,
)
from app.personality.evolution import EvolutionService
from app.services.resident_service import resolve_resident_mentions

logger = logging.getLogger(__name__)

# E-28: cap event-memory content length on store (they're re-injected into every
# later retrieval; an unbounded entry is paid for repeatedly).
EVENT_MEMORY_MAX_CHARS = 80

#: 镇务专用道认的幂等键前缀(``metadata_json["civic_event"]``,写入侧见
#: ``app/services/civic_memory.py``)。**只收结果档** —— 征询档与 world_event
#: 都不进,理由见 ``_fetch_reserved_civic_candidates``。
CIVIC_RESULT_EVENT_PREFIX = "civic:poll_result:"

#: world_event 专用道认的两个判据(写入侧见 ``app/services/world_event_service.py``
#: 的 ``TIER_SUBSTANTIVE``,两侧同一个字面量由
#: ``tests/test_pool_world_event_lane.py`` 钉住)。
#:
#: **不按 ``source`` 单独开道**:生产实测(2026-08-10)公共臂 top-41 全是
#: ``importance=0.5`` 的天气,近 3 天的 world_event 记忆是 weather 261 / festival 17
#: —— 按 source + ``created_at DESC`` 开道等于 94% 抓到天气。所以必须再叠上 W1 落下
#: 的显式档位标记 ``metadata_json["tier"]``。
#:
#: 存量 1380 条没有这个键 → 专用道对它们查不到,这是预期的(**不回填**:数据变更与
#: 开闸不同车)。
WORLD_EVENT_SOURCE = "world_event"
SUBSTANTIVE_TIER = "substantive"

#: 保留位生效的最小候选池深度 = **真实池深**(``_search_events_scored`` 的
#: ``max(limit*3, 30)``,``limit`` 取 ``retrieve_context`` 的默认 10)。
#:
#: 比它小的 cap 只有一个来路:``_search_events`` 那条 fail-open 路径 —— embedding
#: 拿不到时它把 ``limit``(=10)当 cap 传下来。那条路上池只有 10 条**且没有相关度
#: 可言**,在 10 个坑里塞 2 条按 ``created_at DESC`` 盲选的公告 = 20% 的输出被盲
#: 选污染。所以 ``effective_reserve = 0 if cap < POOL_RESERVE_MIN_CAP else reserve``。
POOL_RESERVE_MIN_CAP = 30


def _pool_order_key(m: Memory):
    """候选池定序键:``importance DESC, created_at DESC``(与 SQL 的 ORDER BY 同构)。

    ``created_at`` 归一到 aware:同一个 session 里刚播种的行带 tzinfo,从库里新
    读出来的行(sqlite)可能是 naive,混排会直接 TypeError。
    """
    created = m.created_at
    if created is None:
        return (m.importance or 0.0, datetime.min.replace(tzinfo=UTC))
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (m.importance or 0.0, created)


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is missing."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = num_a = num_b = 0.0
    for i in range(n):
        av = float(a[i]); bv = float(b[i])
        dot += av * bv
        num_a += av * av
        num_b += bv * bv
    if num_a == 0.0 or num_b == 0.0:
        return 0.0
    return dot / (math.sqrt(num_a) * math.sqrt(num_b))


class MemoryService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_memory(
        self,
        resident_id: str,
        type: str,
        content: str,
        importance: float,
        source: str,
        *,
        related_resident_id: str | None = None,
        related_user_id: str | None = None,
        media_url: str | None = None,
        media_summary: str | None = None,
        embedding: list[float] | None = None,
        metadata_json: dict | None = None,
    ) -> Memory:
        """Create and persist a new memory record."""
        # E-28: event memories are re-injected into every later retrieval, so an
        # unbounded entry is paid for over and over. Cap them on store. Longer,
        # bounded-count types (relationship/reflection) keep their full text.
        if type == "event" and content and len(content) > EVENT_MEMORY_MAX_CHARS:
            content = content[:EVENT_MEMORY_MAX_CHARS].rstrip() + "…"
        # Realism P1-12: calibrate event importance by per-resident quantile so a
        # model's score inflation is absorbed (raw preserved in metadata for
        # traceability). Skipped without enough history / for non-event types.
        from app.config import settings
        if settings.realism_enabled and type == "event":
            raw = importance
            importance = await self._normalize_importance(resident_id, raw)
            metadata_json = {**(metadata_json or {}), "raw_importance": round(float(raw), 4)}
        mem = Memory(
            resident_id=resident_id,
            type=type,
            content=content,
            importance=importance,
            source=source,
            related_resident_id=related_resident_id,
            related_user_id=related_user_id,
            media_url=media_url,
            media_summary=media_summary,
            embedding=embedding,
            metadata_json=metadata_json,
        )
        self.db.add(mem)
        await self.db.commit()
        await self.db.refresh(mem)
        # S2/D1: a memory that references a player fires the domain event that
        # drives the "remembered" / "memory_keeper" achievements. Post-commit;
        # handlers run in their own sessions and never affect this write.
        if related_user_id:
            from app.events.bus import emit
            await emit(self.db, "memory_written_about_user",
                       user_id=related_user_id, resident_id=resident_id, memory_id=mem.id)
        return mem

    async def get_memories(
        self,
        resident_id: str,
        *,
        type: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """Get memories for a resident, optionally filtered by type."""
        stmt = select(Memory).where(Memory.resident_id == resident_id)
        if type:
            stmt = stmt.where(Memory.type == type)
        stmt = stmt.order_by(Memory.created_at.desc()).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_relationship(
        self,
        resident_id: str,
        *,
        user_id: str | None = None,
        resident_id_target: str | None = None,
    ) -> Memory | None:
        """Get the relationship memory for a specific person."""
        stmt = select(Memory).where(
            Memory.resident_id == resident_id,
            Memory.type == "relationship",
        )
        if user_id:
            stmt = stmt.where(Memory.related_user_id == user_id)
        elif resident_id_target:
            stmt = stmt.where(Memory.related_resident_id == resident_id_target)
        else:
            return None
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_relationship(
        self,
        resident_id: str,
        *,
        user_id: str | None = None,
        resident_id_target: str | None = None,
        content: str,
        importance: float,
        metadata_json: dict | None = None,
    ) -> Memory:
        """Update an existing relationship memory, or create if not found."""
        existing = await self.get_relationship(
            resident_id, user_id=user_id, resident_id_target=resident_id_target,
        )
        if existing:
            existing.content = content
            existing.importance = importance
            if metadata_json is not None:
                existing.metadata_json = metadata_json
            existing.last_accessed_at = datetime.now(UTC)
            await self.db.commit()
            return existing
        else:
            return await self.add_memory(
                resident_id, "relationship", content, importance,
                "chat_player" if user_id else "chat_resident",
                related_user_id=user_id,
                related_resident_id=resident_id_target,
                metadata_json=metadata_json,
            )

    async def get_recent_reflections(
        self,
        resident_id: str,
        limit: int = 5,
    ) -> list[Memory]:
        """Get most important recent reflections."""
        stmt = (
            select(Memory)
            .where(Memory.resident_id == resident_id, Memory.type == "reflection")
            .order_by(Memory.importance.desc(), Memory.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_events_since_last_reflection(self, resident_id: str) -> int:
        """Count event memories created after the most recent reflection."""
        last_ref_stmt = (
            select(Memory.created_at)
            .where(Memory.resident_id == resident_id, Memory.type == "reflection")
            .order_by(Memory.created_at.desc())
            .limit(1)
        )
        result = await self.db.execute(last_ref_stmt)
        last_ref_time = result.scalar_one_or_none()

        count_stmt = select(func.count()).select_from(Memory).where(
            Memory.resident_id == resident_id,
            Memory.type == "event",
        )
        if last_ref_time:
            count_stmt = count_stmt.where(Memory.created_at > last_ref_time)

        result = await self.db.execute(count_stmt)
        return result.scalar_one()

    async def evict_memories(
        self,
        resident_id: str,
        *,
        importance_floor: float | None = None,
        idle_days: int | None = None,
    ) -> int:
        """Soft-archive stale, low-importance *event* memories (realism P0-2).

        Score-floor semantics (replaces the old hard 500-count cap): an event
        memory with ``importance < floor`` that hasn't been accessed in
        ``idle_days`` is marked ``archived_at`` (kept for provenance, excluded
        from active retrieval). relationship/reflection/dream memories are never
        archived (only ``type == "event"`` is considered).
        """
        from app.config import settings
        floor = settings.realism_evict_importance_floor if importance_floor is None else importance_floor
        days = settings.realism_evict_idle_days if idle_days is None else idle_days
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = select(Memory).where(
            Memory.resident_id == resident_id,
            Memory.type == "event",
            Memory.importance < floor,
            Memory.last_accessed_at < cutoff,
            Memory.archived_at.is_(None),
        )
        rows = list((await self.db.execute(stmt)).scalars().all())
        if rows:
            now = datetime.now(UTC)
            for m in rows:
                m.archived_at = now
            await self.db.commit()
        return len(rows)

    async def _normalize_importance(self, resident_id: str, raw: float) -> float:
        """Realism P1-12: map ``raw`` to its mid-rank quantile within the
        resident's recent event importances (raw values, from metadata so the
        mapping doesn't compound on itself). Returns raw when history < 10."""
        from app.config import settings
        window = settings.realism_importance_window
        rows = (await self.db.execute(
            select(Memory.importance, Memory.metadata_json)
            .where(
                Memory.resident_id == resident_id,
                Memory.type == "event",
                Memory.archived_at.is_(None),
            )
            .order_by(Memory.created_at.desc())
            .limit(window)
        )).all()
        vals: list[float] = []
        for imp, meta in rows:
            rv = (meta or {}).get("raw_importance") if isinstance(meta, dict) else None
            vals.append(float(rv if rv is not None else (imp if imp is not None else 0.0)))
        if len(vals) < 10:
            return round(float(raw), 4)
        below = sum(1 for v in vals if v < raw)
        equal = sum(1 for v in vals if v == raw)
        return round((below + 0.5 * equal) / len(vals), 4)

    async def retrieve_context(
        self,
        resident_id: str,
        *,
        user_id: str | None = None,
        resident_id_target: str | None = None,
        query_text: str = "",
        max_events: int = 10,
        max_reflections: int = 3,
        commit_access: bool = True,
        allow_embedding: bool = True,
    ) -> dict:
        """Retrieve memory context for a conversation.

        Returns dict with keys: relationship, reflections, events.
        """
        # 1. Structured: relationship memory for this person
        relationship = await self.get_relationship(
            resident_id, user_id=user_id, resident_id_target=resident_id_target,
        )

        # 2. Structured: top reflections by importance
        reflections = await self.get_recent_reflections(resident_id, limit=max_reflections)

        # 3. Events: realism scored vector retrieval, else recency+importance
        events = await self._retrieve_events(
            resident_id,
            query_text if allow_embedding else "",
            max_events,
        )

        # Update last_accessed_at for all retrieved memories
        now = datetime.now(UTC)
        all_memories = [m for m in [relationship] + reflections + events if m is not None]
        for mem in all_memories:
            mem.last_accessed_at = now
        if all_memories and commit_access:
            await self.db.commit()
        elif all_memories:
            await self.db.flush()

        return {
            "relationship": relationship,
            "reflections": reflections,
            "events": events,
        }

    async def _fetch_personal_candidates(
        self,
        resident_id: str,
        cap: int,
        exclude_ids: list[str] | None = None,
    ) -> list[Memory]:
        """Top-`cap` non-archived event memories by static importance/recency.

        `exclude_ids` 为空时这条语句与保留位落地**之前**逐字相同 —— 这是
        ``REALISM_POOL_CIVIC_RESERVE=0`` 那条回滚路径的实现依据
        (``tests/test_pool_reserved_slots.py`` 拿 ``git show master:`` 装出改前
        的实现,对拍返回的 **id 序列**)。
        """
        stmt = (
            select(Memory)
            .where(
                Memory.resident_id == resident_id,
                Memory.type == "event",
                Memory.archived_at.is_(None),
            )
        )
        if exclude_ids:
            stmt = stmt.where(Memory.id.notin_(exclude_ids))
        stmt = (
            stmt
            .order_by(Memory.importance.desc(), Memory.created_at.desc())
            .limit(cap)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _fetch_reserved_civic_candidates(
        self, resident_id: str, cap: int,
    ) -> list[Memory]:
        """镇务专用道:最近 N 条**结果档**镇务记忆,``N = effective_reserve``。

        只收结果档(``civic:poll_result:%``)。征询档(raw 0.6)不进 —— 保持 M3
        分档,且「镇上正在议什么」已由事实层的 ``town_facts.open_polls`` 提供;
        ``world_event`` 也不进 —— 生产实测公共臂 top-41 全是 ``importance=0.5``
        的天气,10 个公共坑会 100% 被天气占满。

        道内按 ``created_at DESC``:镇务要的是「刚发生什么」。按 importance 排会
        让上周的选举压住今天的结果(结果档 raw 全是 0.9,归一落点相近)。

        JSON 路径用 ``metadata_json["civic_event"].as_string().like(...)`` ——
        PG 的 ``->>`` 与 sqlite 的 ``JSON_EXTRACT`` 都编得出来(K17)。
        """
        from app.config import settings
        reserve = settings.realism_pool_civic_reserve
        # fail-open 路径(``_search_events`` 把 limit=10 当 cap 传下来)上池只有
        # 10 条**且没有相关度可言**,在里面塞 2 条盲选公告 = 20% 的输出被污染。
        effective_reserve = 0 if cap < POOL_RESERVE_MIN_CAP else max(reserve, 0)
        effective_reserve = min(effective_reserve, cap)
        return await self._query_recent_civic_results(resident_id, effective_reserve)

    async def _query_recent_civic_results(
        self, resident_id: str, limit: int,
    ) -> list[Memory]:
        """镇务结果档的**取数判据**:最近 ``limit`` 条,``created_at DESC``。

        判据(``civic:poll_result:%``)与排序的理由见调用方
        ``_fetch_reserved_civic_candidates``。单独抽出来是因为它有**第二个**调用方:
        计划阶段(``agent/phases/plan/basic.py``)也要「镇上最近出了什么事」,而它
        的条数由另一个闸(``realism_plan_public_memories``)定,与候选池的保留位无关。
        两处共用同一份 SQL,判据就不会各自漂。
        """
        if limit <= 0:
            return []
        stmt = (
            select(Memory)
            .where(
                Memory.resident_id == resident_id,
                Memory.type == "event",
                Memory.archived_at.is_(None),
                Memory.metadata_json["civic_event"].as_string().like(
                    f"{CIVIC_RESULT_EVENT_PREFIX}%"),
            )
            .order_by(Memory.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _fetch_reserved_world_event_candidates(
        self, resident_id: str, cap: int,
    ) -> list[Memory]:
        """world_event 专用道:最近 N 条**实质档**世界事件记忆,``N = effective_reserve``。

        与镇务道同构,只有判据不同:这里认 ``source='world_event'`` **且**
        ``metadata_json->>'tier' = 'substantive'``。只判 source 会 94% 抓到天气
        (理由见 ``WORLD_EVENT_SOURCE`` 的注释);只判 tier 则会把将来任何别的写入侧
        盖的同名标记也收进来 —— 两个条件一起才是「这条道要的东西」。

        **设计反转**:有了专用道之后,实质世界事件**保持低 importance**(与琐事同档,
        永不挤占个人臂),完全靠这条道拿到**候选池**里的保证坑位。抬 importance 的
        老办法(``REALISM_EVENT_MEMORY_TIERED``)会让实质事件**同时**挤占个人臂
        (12 周饱和实测 day28 7/30 → day84 21/30)**又**吃专用道坑位 —— 双重占坑,
        已被本道取代,那个闸门应当保持永久关闭。

        ⚠️ 这条道保证的是**进候选池**,不是「保证被检索到」。池是 30 条,而
        ``retrieve_context`` 交给 prompt 的是 ``_search_events_scored`` 打分后的
        **top-10** —— 进池只是拿到参赛资格。世界事件带上 embedding 之后
        (``world_event_service._embed_or_none``)仍要 ``cos`` 高到把第 10 名个人
        记忆比下去才进得了 prompt(饱和史下实测:切题问句 0.6715 > 0.6061 进得去,
        不切题 0.4871 进不去)。这**是想要的行为**:专用道给的是入场券,相关度决定
        谁真的被想起来。核验开闸效果要走 ``retrieve_context`` 的返回值,查「进没进
        top-30」会拿到假阳性。

        道内按 ``created_at DESC``:世界事件要的是「最近发生了什么」。按 importance
        排在这条道上等于随机 —— 反转之后道内的 importance 本来就全等。

        JSON 路径用 ``metadata_json["tier"].as_string()`` —— PG 的 ``->>`` 与 sqlite
        的 ``JSON_EXTRACT`` 都编得出来(K17)。
        """
        from app.config import settings
        reserve = settings.realism_pool_world_event_reserve
        # fail-open 路径(``_search_events`` 把 limit=10 当 cap 传下来)上两条道都
        # 不生效:那时池只有 10 条且没有相关度可言。
        effective_reserve = 0 if cap < POOL_RESERVE_MIN_CAP else max(reserve, 0)
        effective_reserve = min(effective_reserve, cap)
        return await self._query_recent_substantive_world_events(
            resident_id, effective_reserve)

    async def _query_recent_substantive_world_events(
        self, resident_id: str, limit: int,
    ) -> list[Memory]:
        """实质世界事件的**取数判据**:最近 ``limit`` 条,``created_at DESC``。

        两个条件(``source='world_event'`` **且** ``tier='substantive'``)缺一不可,
        理由见调用方 ``_fetch_reserved_world_event_candidates``:只判 source 会 94%
        抓到天气。抽出来的理由与镇务那条同构 —— 计划阶段共用这份判据。
        """
        if limit <= 0:
            return []
        stmt = (
            select(Memory)
            .where(
                Memory.resident_id == resident_id,
                Memory.type == "event",
                Memory.archived_at.is_(None),
                Memory.source == WORLD_EVENT_SOURCE,
                Memory.metadata_json["tier"].as_string() == SUBSTANTIVE_TIER,
            )
            .order_by(Memory.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_public_memories(self, resident_id: str, limit: int) -> list[Memory]:
        """「镇上最近出了什么事」:两条道各取最近的,**轮流**并到 ``limit`` 条。

        给**计划阶段**用(``agent/phases/plan/basic.py``)。它与
        ``retrieve_context`` 是两条路,不该互相借用:

        - 计划阶段没有 query text —— 它要的是「最近发生了什么」,不是「与某句话相关
          的是什么」,相关度那 0.45 分在这里无意义;
        - 也不是「把个人近况的 ``limit=20`` 调大」:每人每天写 480-545 条 event 记忆,
          要覆盖一周就是 ~3500 条一次全表读,而那 20 分钟窗口对**个人近况**本来就是
          对的口径。两件事口径不同,不该用同一个 limit 表达。

        **轮流**而不是把两条道合起来按 ``created_at DESC`` 排:结票那天镇务道会连出
        好几条,纯按时间排会把世界事件整个挤掉,而计划阶段要的正是「镇上出了什么事」
        的两个方面。``limit=2`` 且两条道都有货时,恰好一边一条。

        条数上限由调用方给(``settings.realism_plan_public_memories``),**不吃**
        ``REALISM_POOL_*`` 那两个闸 —— 那两个管的是检索候选池。
        """
        if limit <= 0:
            return []
        civic = await self._query_recent_civic_results(resident_id, limit)
        world = await self._query_recent_substantive_world_events(resident_id, limit)
        out: list[Memory] = []
        seen: set[str] = set()
        for pair in zip_longest(civic, world):
            for m in pair:
                if m is None or m.id in seen:
                    continue
                seen.add(m.id)
                out.append(m)
                if len(out) >= limit:
                    return out
        return out

    async def _fetch_event_candidates(self, resident_id: str, cap: int) -> list[Memory]:
        """候选池 = 镇务专用道 ∪ world_event 专用道 ∪ 个人臂,共 `cap` 个坑。

        候选池是 ``ORDER BY importance DESC, created_at DESC LIMIT cap`` ——
        打分公式里那 0.45 的相关度权重是**截断之后**才参与的,于是它根本不参与
        入池。生产实测:池底顶到 1.0 的居民(jiang-lin 8355 event / zhao-qiwen
        8042)永远收不到归一 0.99 的镇务结果记忆,**差 0.01,病症逐人**。

        **不扩池**(扩池会稀释 ``public/pool < 2/3`` 那条硬门的分母),而是在池内
        留位。四条不变量,**对两条道同时成立**:

        1. ``len(pool)`` 恒等于 ``min(cap, 活跃 event 总数)`` —— 两条道**各自没填
           满的坑都退还给个人臂**:个人臂的 limit 是
           ``cap - (civic实拿 + world_event实拿)``,用的是**实拿条数**,不是两个
           reserve 配置值之和。否则还没结过票、还没出过实质事件的世界会拿到一个
           27 条的池;
        2. 两条道的成员都从个人臂**排除**,不双份占坑;两条道**之间**也不重叠 ——
           判据互斥(``civic_event`` 前缀 vs ``source='world_event'`` + tier),由
           ``tests/test_pool_world_event_lane.py`` 直接量两条道的交集钉住,而不是在
           这里加一行去重把它掩盖过去;
        3. 合并后仍按 ``importance DESC, created_at DESC`` 交给打分层,打分公式
           一字不动 —— 保留位改的是「谁进池」,不是「怎么排」;
        4. ``cap < POOL_RESERVE_MIN_CAP`` 的 fail-open 路径上**两条道都不生效**。

        预算:``civic_reserve=2`` + ``we_reserve=1`` → public 3/30 = 10%,个人臂 27,
        离 ``public/pool < 2/3`` 与 ``personal > 1/3`` 两条硬门仍有大余量
        (``tests/test_civic_prompt_budget.py`` 在饱和史下实测)。
        """
        civic = await self._fetch_reserved_civic_candidates(resident_id, cap)
        # world_event 道拿 civic 道之后剩下的坑。传 ``cap`` 而不是 ``cap - len(civic)``
        # 是有意的:那个参数同时决定 fail-open 判据(``cap < POOL_RESERVE_MIN_CAP``),
        # 减完再传会让 cap=30 的正路被误判成 fail-open,两条道一起哑掉。
        world = await self._fetch_reserved_world_event_candidates(resident_id, cap)
        world = world[:max(cap - len(civic), 0)]
        reserved = civic + world
        personal = await self._fetch_personal_candidates(
            resident_id, cap - len(reserved),
            exclude_ids=[m.id for m in reserved])
        if not reserved:
            # 两条道都空(闸关,或闸开但一条都查不到)的逐字节旧路径:
            # 一次查询,一个未经重排的结果集。
            return personal
        return sorted(reserved + personal, key=_pool_order_key, reverse=True)

    async def _search_events(
        self,
        resident_id: str,
        query_text: str,
        limit: int = 10,
    ) -> list[Memory]:
        """Search event memories. Falls back to recency+importance ranking."""
        return await self._fetch_event_candidates(resident_id, limit)

    async def _retrieve_events(
        self,
        resident_id: str,
        query_text: str,
        limit: int,
    ) -> list[Memory]:
        """Realism P0-2: scored vector retrieval when enabled + query + embedding
        available; else static importance/recency (fail-open)."""
        from app.config import settings
        if settings.realism_enabled and query_text:
            try:
                emb = await generate_embedding(query_text)
                if emb:
                    return await self._search_events_scored(resident_id, emb, limit)
            except Exception as e:
                logger.debug("scored retrieval fell back: %s", e)
        return await self._search_events(resident_id, "", limit)

    async def _search_events_scored(
        self,
        resident_id: str,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[Memory]:
        """Rank candidates by Generative-Agents weighting:
        ``0.45×relevance + 0.30×recency + 0.25×importance`` where
        ``recency = exp(-Δt_hours / τ)``, ``τ = 72h × (1 + importance)``.

        Cosine relevance is computed in Python over the top-K static candidates
        (works identically on sqlite tests and pgvector prod; no dead-code path).
        """
        from app.config import settings
        candidates = await self._fetch_event_candidates(resident_id, cap=max(limit * 3, 30))
        if not candidates:
            return []
        now = datetime.now(UTC)
        wr = settings.realism_retrieval_relevance_weight
        wc = settings.realism_retrieval_recency_weight
        wi = settings.realism_retrieval_importance_weight
        tau_base = settings.realism_recency_tau_hours
        scored: list[tuple[float, Memory]] = []
        for m in candidates:
            imp = m.importance or 0.0
            rel = _cosine(query_embedding, m.embedding)
            created = m.created_at if m.created_at.tzinfo else m.created_at.replace(tzinfo=UTC)
            dt_h = max((now - created).total_seconds() / 3600.0, 0.0)
            tau = tau_base * (1.0 + imp)
            recency = math.exp(-dt_h / tau) if tau > 0 else 0.0
            scored.append((wr * rel + wc * recency + wi * imp, m))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [m for _, m in scored[:limit]]

    async def search_events_vector(
        self,
        resident_id: str,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[Memory]:
        """Search event memories using pgvector cosine similarity.

        For PostgreSQL with pgvector only. Falls back to _search_events() if unavailable.
        """
        try:
            from sqlalchemy import text
            stmt = text("""
                SELECT id, content, importance, source, created_at, last_accessed_at,
                       metadata_json, media_url, media_summary,
                       1 - (embedding <=> :query_vec) AS similarity
                FROM memories
                WHERE resident_id = :rid AND type = 'event' AND embedding IS NOT NULL
                      AND archived_at IS NULL
                ORDER BY embedding <=> :query_vec
                LIMIT :lim
            """)
            result = await self.db.execute(stmt, {
                "rid": resident_id,
                "query_vec": str(query_embedding),
                "lim": limit,
            })
            rows = result.fetchall()
            if not rows:
                return await self._search_events(resident_id, "", limit)

            ids = [row[0] for row in rows]
            mem_stmt = select(Memory).where(Memory.id.in_(ids))
            mem_result = await self.db.execute(mem_stmt)
            memories = {m.id: m for m in mem_result.scalars().all()}
            return [memories[id] for id in ids if id in memories]
        except Exception as e:
            logger.debug("pgvector search unavailable, falling back: %s", e)
            return await self._search_events(resident_id, "", limit)

    async def extract_events(
        self,
        resident: "Resident",
        other_name: str,
        conversation_text: str,
        *,
        source: str = "chat_player",
    ) -> list[Memory]:
        """Extract event memories from a conversation using LLM."""
        sbti_data = (resident.meta_json or {}).get("sbti")
        coloring = sbti_coloring_block(sbti_data)

        system = EXTRACT_EVENTS_SYSTEM.format(sbti_coloring=coloring)
        user_msg = EXTRACT_EVENTS_USER.format(
            resident_name=resident.name,
            other_name=other_name,
            conversation_text=conversation_text,
        )

        try:
            raw = await llm_chat(
                system, [{"role": "user", "content": user_msg}], max_tokens=500,
                meter=Meter(scenario="extract", resident_id=resident.id), expects_json=True,
            )
            data = extract_json_object(raw)
            if data is None:
                raise ValueError("no JSON object in LLM response")
        except Exception as e:
            logger.warning("Event extraction failed: %s", e)
            return []

        items = data.get("memories", [])
        mentioned = [i.get("mentioned_resident") for i in items
                     if isinstance(i, dict) and i.get("mentioned_resident")]
        mention_map = await resolve_resident_mentions(self.db, mentioned) if mentioned else {}

        memories = []
        for item in items:
            content = item.get("content", "")
            importance = float(item.get("importance", 0.5))
            if not content:
                continue

            related_id = mention_map.get((item.get("mentioned_resident") or "").strip())
            if related_id == resident.id:
                related_id = None  # 不指向自己

            emb = await generate_embedding(content)
            mem = await self.add_memory(
                resident_id=resident.id,
                type="event",
                content=content,
                importance=importance,
                source=source,
                embedding=emb,
                related_resident_id=related_id,
            )
            memories.append(mem)

        # Evolution hooks (non-blocking)
        await self._run_evolution_hooks(resident, memories)

        return memories

    async def _run_evolution_hooks(self, resident: "Resident", memories: list[Memory]) -> None:
        """Conditionally trigger personality shift/drift from freshly-extracted
        event memories. Shared by extract_events and process_chat_wrapup."""
        if not memories or resident is None:
            return
        evo = EvolutionService(self.db)

        # Realism P1-12: shift gate is now a double condition — normalized
        # importance percentile ≥ P95 AND |valence| > 0.5 — so a model's score
        # inflation alone can't chain-trigger personality shifts (diagnosis
        # "no-calibration single point"). Off = legacy raw importance≥0.9.
        from app.config import settings
        if settings.realism_enabled:
            valence = abs(float((resident.mood_json or {}).get("valence", 0.0)))
            candidates = [m for m in memories if (m.importance or 0.0) >= settings.realism_shift_percentile]
            trigger = candidates[0] if (candidates and valence > settings.realism_shift_valence_gate) else None
        else:
            high_importance = [m for m in memories if m.importance >= 0.9]
            trigger = high_importance[0] if high_importance else None
        shifted = None
        if trigger is not None:
            try:
                shifted = await evo.evaluate_shift(resident, trigger)
            except Exception as e:
                logger.warning("Shift evaluation error (non-fatal): %s", e)

        # One evidence batch has one evolution meaning.  A successful dramatic
        # shift must not immediately consume another two dimensions as drift.
        if shifted is not None:
            return

        # Check drift trigger: count total events since last drift
        total_events = await self.count_events_since_last_reflection(resident.id)
        if total_events >= 15:
            try:
                await evo.evaluate_drift(resident)
            except Exception as e:
                logger.warning("Drift evaluation error (non-fatal): %s", e)

    async def process_chat_wrapup(
        self,
        initiator: "Resident",
        target: "Resident",
        dialog_text: str,
    ) -> dict:
        """One-call resident-resident chat wrap-up (E-04/E-05).

        Replaces the old five wrap-up LLM calls (extract×2 + update_relationship×2
        + summary) with a single merged call — the dialog is sent once instead of
        five times. Retries once on parse failure, then falls back to a generic
        summary (never blank-screens). Persists both residents' event memories
        (with embeddings + evolution hooks) and relationship updates, and returns
        the broadcast ``{summary, mood}``.
        """
        init_rel = await self.get_relationship(initiator.id, resident_id_target=target.id)
        tgt_rel = await self.get_relationship(target.id, resident_id_target=initiator.id)
        user_msg = CHAT_WRAPUP_USER.format(
            initiator_name=initiator.name,
            target_name=target.name,
            initiator_relationship=(init_rel.content if init_rel else "（首次接触，尚无关系记忆）"),
            target_relationship=(tgt_rel.content if tgt_rel else "（首次接触，尚无关系记忆）"),
            conversation_text=dialog_text,
        )

        data = None
        for attempt in (1, 2):  # E-05: one retry on parse failure
            try:
                raw = await llm_chat(
                    CHAT_WRAPUP_SYSTEM, [{"role": "user", "content": user_msg}], max_tokens=800,
                    meter=Meter(scenario="chat_wrapup", resident_id=initiator.id, attempt_no=attempt),
                    expects_json=True,
                )
                data = extract_json_object(raw)
            except Exception as e:
                logger.warning("Chat wrap-up call failed (attempt %d): %s", attempt, e)
                data = None
            if data is not None:
                break

        fallback = {"summary": f"{initiator.name} 和 {target.name} 聊了一会儿", "mood": "neutral"}
        if data is None:
            return fallback

        init_side = data.get("initiator") or {}
        tgt_side = data.get("target") or {}
        mentioned = [
            i.get("mentioned_resident")
            for side in (init_side, tgt_side)
            for i in (side.get("memories") or [])
            if isinstance(i, dict) and i.get("mentioned_resident")
        ]
        mention_map = await resolve_resident_mentions(self.db, mentioned) if mentioned else {}

        await self._persist_wrapup_side(initiator, target, init_side, mention_map)
        await self._persist_wrapup_side(target, initiator, tgt_side, mention_map)

        mood = data.get("mood")
        mood = mood if mood in ("positive", "neutral", "negative") else "neutral"

        # S1-3: feed the already-extracted mood into opinion dynamics (zero
        # extra LLM — the one wrapup call above is the only call). Best-effort
        # + gated: an opinion failure must never break the chat wrapup.
        try:
            from app.config import settings as _opinion_settings
            if _opinion_settings.polis_opinion_enabled:
                from app.services.opinion_service import OpinionService
                await OpinionService(self.db).update_from_chat(
                    initiator.slug, target.slug, mood,
                )
        except Exception:
            logger.warning("opinion update from chat wrapup failed", exc_info=True)

        return {
            "summary": str(data.get("summary") or fallback["summary"]),
            "mood": mood,
        }

    async def _persist_wrapup_side(
        self, resident: "Resident", other: "Resident", side: dict,
        mention_map: dict[str, str] | None = None,
    ) -> None:
        """Persist one resident's extracted memories + relationship from the
        merged wrap-up result, then run evolution hooks."""
        memories: list[Memory] = []
        for item in (side.get("memories") or []):
            content = (item.get("content") or "").strip()
            if not content:
                continue
            importance = float(item.get("importance", 0.5))
            raw_mention = (item.get("mentioned_resident") or "").strip()
            related_id = (mention_map or {}).get(raw_mention)
            if not related_id or related_id == resident.id:
                related_id = other.id  # 默认：记忆关于对话对象
            emb = await generate_embedding(content)
            mem = await self.add_memory(
                resident_id=resident.id, type="event", content=content,
                importance=importance, source="chat_resident", embedding=emb,
                related_resident_id=related_id,
            )
            memories.append(mem)

        rel = side.get("relationship") or {}
        rel_content = (rel.get("content") or "").strip() if isinstance(rel, dict) else ""
        if rel_content:
            meta = rel.get("metadata")
            await self.update_relationship(
                resident.id, resident_id_target=other.id,
                content=rel_content, importance=float(rel.get("importance", 0.5)),
                metadata_json=meta if isinstance(meta, dict) else None,
            )

        await self._run_evolution_hooks(resident, memories)

    async def update_relationship_via_llm(
        self,
        resident: "Resident",
        other_name: str,
        event_summaries: list[str],
        *,
        user_id: str | None = None,
        resident_id_target: str | None = None,
    ) -> Memory:
        """Update relationship memory using LLM analysis."""
        existing = await self.get_relationship(
            resident.id, user_id=user_id, resident_id_target=resident_id_target,
        )
        current_rel = existing.content if existing else "（首次接触，尚无关系记忆）"

        sbti_data = (resident.meta_json or {}).get("sbti")
        coloring = sbti_coloring_block(sbti_data)

        system = UPDATE_RELATIONSHIP_SYSTEM.format(sbti_coloring=coloring)
        user_msg = UPDATE_RELATIONSHIP_USER.format(
            resident_name=resident.name,
            other_name=other_name,
            current_relationship=current_rel,
            event_summaries="\n".join(f"- {s}" for s in event_summaries),
        )

        try:
            raw = await llm_chat(
                system, [{"role": "user", "content": user_msg}], max_tokens=300,
                meter=Meter(scenario="update_rel", resident_id=resident.id), expects_json=True,
            )
            data = extract_json_object(raw)
            if data is None:
                raise ValueError("no JSON object in LLM response")
        except Exception as e:
            logger.warning("Relationship update failed: %s", e)
            if existing:
                return existing
            return await self.add_memory(
                resident.id, "relationship", f"Met {other_name}",
                0.3, "chat_player" if user_id else "chat_resident",
                related_user_id=user_id, related_resident_id=resident_id_target,
            )

        return await self.update_relationship(
            resident.id,
            user_id=user_id,
            resident_id_target=resident_id_target,
            content=data.get("content", f"Met {other_name}"),
            importance=float(data.get("importance", 0.5)),
            metadata_json=data.get("metadata"),
        )

    async def generate_reflections(self, resident: "Resident") -> list[Memory]:
        """Generate reflection memories from recent events and relationships."""
        recent_events = await self.get_memories(resident.id, type="event", limit=20)
        relationships = await self.get_memories(resident.id, type="relationship", limit=10)

        if not recent_events:
            return []

        sbti_data = (resident.meta_json or {}).get("sbti")
        coloring = sbti_coloring_block(sbti_data)

        events_text = "\n".join(f"- [{e.source}] {e.content}" for e in recent_events)
        rels_text = "\n".join(f"- {r.content}" for r in relationships) if relationships else "（尚无关系记忆）"

        system = REFLECT_SYSTEM.format(sbti_coloring=coloring)
        user_msg = REFLECT_USER.format(
            resident_name=resident.name,
            recent_events=events_text,
            relationships=rels_text,
        )

        try:
            raw = await llm_chat(
                system, [{"role": "user", "content": user_msg}], max_tokens=400,
                meter=Meter(scenario="reflect", resident_id=resident.id), expects_json=True,
            )
            data = extract_json_object(raw)
            if data is None:
                raise ValueError("no JSON object in LLM response")
        except Exception as e:
            logger.warning("Reflection generation failed: %s", e)
            return []

        reflections = []
        for item in data.get("reflections", []):
            content = item.get("content", "")
            importance = float(item.get("importance", 0.6))
            if not content:
                continue
            mem = await self.add_memory(
                resident.id, "reflection", content, importance, "reflection",
            )
            reflections.append(mem)

        return reflections
