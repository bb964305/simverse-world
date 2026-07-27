"""F2 公民权晋升 —— 夜间任务的判定层（snapshot + 纯函数）。

设计要点（三条，都对应一个会让机制失效的坑）：

1. **snapshot 语义**。pass 开始时一次性读出 ``{resident: (档位, 锚点, 达标
   同伴)}`` 并冻结，所有判定基于快照，所有写入在 pass 末尾一次 commit，中途
   绝不重读选民集。否则结果依赖数据库行序，同一状态多次运行得到不同不动点。
2. **判定是纯函数**。输入快照 → 输出待升 id 集合，可以在内存里打乱顺序反复
   跑并断言输出恒等——不要试图在 Postgres 上控制行序。
3. **锚定公民集不自指**。同伴取自「内置阵容 ∪ 已过考察期的归化公民」，不是
   活的 ``is_civic_voter``；否则转移函数自指，产生级联升降与「脱锚公民团」
   （某人的 N 位同伴全是刚晋升的 UGC、零条内置边）。

时间尺度：门槛一律走**世界日**（``app/world_clock.py`` 是唯一入口，k=4），
而 familiarity 的衰减用的是**真实日**（``realism_rel_decay_idle_days = 30``）
——这是有意的两套尺度，实现不得擅自统一。

夜间任务**只升，永不自动降**（见 ``civic_membership.auto_demotion_enabled``
的 docstring）。撤销是显式事件，走 ``civic_membership.revoke_citizenship``。

⚠️ 已知的结构性偏置（风险项，不是阈值问题）：``extravert`` 档的
``SpontaneousDecidePlugin`` 用加权采样挑聊天对象，权重与既有熟识度正相关，
系统性歧视新人。若标定发现晋升面长期为空，根因可能在采样而不在阈值——所以
:func:`promotion_evidence` 输出 top-familiarity 分布，而不只是达标计数。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

from app.services.civic_membership import (
    CITIZEN,
    CIVIC_VOTER_TYPES,
    SYSTEM_CREATOR_ID,
    UGC_RESIDENT_TYPE,
    is_ugc_resident,
)

logger = logging.getLogger(__name__)

#: T2 存量回填的完成标记（``system_config``）。降级 anchor 路径读它。
BACKFILL_MARK_KEY = "civic_backfill_done"

#: 关系表里「居民」这一侧的 party 类型（另一种是 "player"）。
_RESIDENT_PARTY = "resident"

#: 三态旋钮 ``CIVIC_PROMOTION_MODE`` 的取值（``civic_membership.promotion_mode``）。
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"

#: Task 2 三个门槛的**占位值指纹**（``_DEFAULT_MIN_WORLD_DAYS`` /
#: ``_DEFAULT_MIN_PEERS`` / ``_DEFAULT_MIN_FAMILIARITY`` 的当前取值）。
#: 有测试断言它与那三个默认值逐字相等——那条断言是**绊线**：真标定完把默认值
#: 换成实测值时测试会红，改指纹的那一笔 diff 就是「这组数字是量出来的」的
#: 书面承认。
PLACEHOLDER_THRESHOLDS: tuple[float, int, float] = (30.0, 3, 0.20)

#: 显式标定凭据（报告日期 / commit）。只在「实测分布恰好落在占位值上」这种
#: 罕见情形下需要——没有它，一个合法标定结果将永远无法开闸。
CALIBRATION_ACK_ENV = "CIVIC_THRESHOLDS_CALIBRATED"

_ACK_FALSEY = {"", "0", "false", "no", "off", "none", "null"}


class UncalibratedThresholds(RuntimeError):
    """在**门槛还是占位值**的情况下试图走真开闸路径（``mode="on"``）。

    刻意不继承 ``CivicStandingRefused``：那个异常是「这一位居民的这次档位变更
    被防呆拒绝」，pass 里很可能有 ``except CivicStandingRefused: continue`` 之类
    的逐人处理；本异常是「整个判定的入参没资格用来写库」，必须整批中止而不是
    被逐人吞掉。
    """


def _calibration_ack() -> str | None:
    """``CIVIC_THRESHOLDS_CALIBRATED`` 的凭据文本；未设/空/假值 → None。

    刻意不走 ``Settings`` 兜底：这是一次**人为声明**，只能来自部署时显式写下的
    环境变量，不该有代码里的默认值可继承。
    """
    raw = (os.environ.get(CALIBRATION_ACK_ENV) or "").strip()
    return None if raw.lower() in _ACK_FALSEY else raw


def thresholds_are_placeholders(*, min_world_days: float, min_peers: int,
                                min_familiarity: float) -> bool:
    """这三个数字是不是原封不动的占位值（= 从来没有被标定过）。

    判定按**整组**：只要有一个被动过，就说明有人真的看过分布——闸门的目标是
    「拍出来的一整套默认值被当成已标定值」，不是逐个数字的真伪鉴定。
    """
    return (float(min_world_days), int(min_peers), float(min_familiarity)) == (
        float(PLACEHOLDER_THRESHOLDS[0]), int(PLACEHOLDER_THRESHOLDS[1]),
        float(PLACEHOLDER_THRESHOLDS[2]))


def assert_thresholds_calibrated(*, mode: str, min_world_days: float,
                                 min_peers: int, min_familiarity: float) -> None:
    """开闸前置：``mode="on"`` 且三个门槛仍是占位值 → :exc:`UncalibratedThresholds`。

    spec §4.2 要求门槛由实测分布标定，不许拍数字。本模块是这三个旋钮的**第一个
    调用点**，所以这条纪律在这里第一次变成可执行的约束——教训是
    ``rep_credit_min_score = -0.3``：它拍了一个数字，于是拒绝面长期 0/13，闸门
    形同虚设，而没有任何机制在开闸那天喊出来。

    ``off`` / ``shadow`` 一律放行：标定报告（``scripts/civic_calibration_report``）
    与 shadow 名单**恰恰要在占位值上跑**，挡住它们就没人能量出真值了。

    两条合法出口：① 把门槛改成实测值（指纹不再匹配）；② 实测恰好落在占位值上
    时，在环境里写下 ``CIVIC_THRESHOLDS_CALIBRATED=<报告日期/commit>``。
    """
    if str(mode).strip().lower() != MODE_ON:
        return
    if not thresholds_are_placeholders(min_world_days=min_world_days,
                                       min_peers=min_peers,
                                       min_familiarity=min_familiarity):
        return
    ack = _calibration_ack()
    if ack:
        logger.warning(
            "civic promotion 开闸使用的门槛与占位值相同，凭 %s=%r 放行 "
            "(min_world_days=%s min_peers=%s min_familiarity=%s)",
            CALIBRATION_ACK_ENV, ack, min_world_days, min_peers,
            min_familiarity)
        return
    raise UncalibratedThresholds(
        f"拒绝在未标定的门槛上开闸：(min_world_days, min_peers, "
        f"min_familiarity) = ({min_world_days}, {min_peers}, "
        f"{min_familiarity}) 与占位值 {PLACEHOLDER_THRESHOLDS} 逐字相同。"
        f"spec §4.2 要求门槛由实测分布标定（使晋升面非空且非全量）——先跑 "
        f"scripts/civic_calibration_report.py 量出真值再改这三个旋钮；"
        f"若实测确实落在占位值上，用 {CALIBRATION_ACK_ENV}=<报告日期/commit> "
        f"显式声明。mode={MODE_SHADOW!r} 不受本闸门限制。"
    )


@dataclass(frozen=True)
class ResidentFact:
    """快照里的一行居民事实。冻结（frozen）以保证纯函数不能改写输入。"""

    resident_id: str
    slug: str
    resident_type: str
    #: ``creator_id == SYSTEM_CREATOR_ID``（provenance 判定的主键；
    #: ``meta_json.origin == "preset"`` 不可用——admin 创建的 preset 同值）
    is_builtin: bool
    is_ugc: bool
    #: 公民时钟锚点（世界时间）
    anchor_world: datetime
    #: 最近一次晋升的世界时间；None = 从未晋升
    promoted_world: datetime | None
    #: civic_ban sticky 剥夺位（v1 只读不写，候选面从第一天起排除它）
    banned: bool = False


@dataclass(frozen=True)
class PromotionSnapshot:
    now_world: datetime
    facts: tuple[ResidentFact, ...]
    #: 无向边 ``(party_a, party_b, familiarity)``，只含 resident-resident
    familiarity: tuple[tuple[str, str, float], ...]


# ── 纯判定 ─────────────────────────────────────────────────────────────

def anchored_citizen_ids(snap: PromotionSnapshot, *,
                         seasoning_days: float) -> frozenset[str]:
    """锚定公民集 = 内置阵容 ∪ 已过考察期的归化公民（都必须当前在 citizen 档）。"""
    out: set[str] = set()
    for fact in snap.facts:
        if fact.resident_type not in CIVIC_VOTER_TYPES:
            continue
        if fact.is_builtin:
            out.add(fact.resident_id)
            continue
        if (fact.promoted_world is not None
                and (snap.now_world - fact.promoted_world)
                >= timedelta(days=seasoning_days)):
            out.add(fact.resident_id)
    return frozenset(out)


def qualified_peers(snap: PromotionSnapshot, anchors: frozenset[str],
                    threshold: float) -> dict[str, frozenset[str]]:
    """``{resident_id: 与之 familiarity ≥ threshold 的锚定公民集合}``。

    边是无向的（``relation_service.canonical_pair`` 规范化过），两个方向都认。
    """
    acc: dict[str, set[str]] = {}
    for party_a, party_b, fam in snap.familiarity:
        if fam < threshold:
            continue
        if party_b in anchors and party_a != party_b:
            acc.setdefault(party_a, set()).add(party_b)
        if party_a in anchors and party_a != party_b:
            acc.setdefault(party_b, set()).add(party_a)
    return {k: frozenset(v) for k, v in acc.items()}


def select_promotions(
    snap: PromotionSnapshot, *, min_world_days: float, min_peers: int,
    min_familiarity: float, seasoning_days: float, mode: str = MODE_SHADOW,
) -> tuple[str, ...]:
    """待晋升的居民 id，**按 id 升序**（顺序无关性 + 单夜上限的确定性截断）。

    ``mode`` 只影响一件事：:func:`assert_thresholds_calibrated` 这道开闸前置。
    默认 ``"shadow"``（观测态，任何门槛值都放行）——夜间任务真要写库时必须
    显式传 ``mode="on"``，那一态下原封不动的占位门槛会被拒绝。判定本身与
    ``mode`` 无关：同一组入参在三态下选出同一批人。
    """
    assert_thresholds_calibrated(
        mode=mode, min_world_days=min_world_days, min_peers=min_peers,
        min_familiarity=min_familiarity)
    anchors = anchored_citizen_ids(snap, seasoning_days=seasoning_days)
    peers = qualified_peers(snap, anchors, min_familiarity)
    picked: list[str] = []
    for fact in snap.facts:
        if not fact.is_ugc:
            continue
        if fact.resident_type != UGC_RESIDENT_TYPE:
            continue                      # 只有 denizen 档进候选面
        if fact.banned:
            continue                      # civic_ban：sticky，永不自动复籍
        age_world_days = (snap.now_world - fact.anchor_world) / timedelta(days=1)
        if age_world_days < min_world_days:
            continue
        if len(peers.get(fact.resident_id, frozenset())) < min_peers:
            continue
        picked.append(fact.resident_id)
    return tuple(sorted(picked))


def select_promotions_for_write(
    snap: PromotionSnapshot, *, min_world_days: float, min_peers: int,
    min_familiarity: float, seasoning_days: float,
) -> tuple[str, ...]:
    """夜间任务写路径的**唯一**入口——真正调用
    :func:`app.services.civic_membership.grant_citizenship_batch` 之前，候选
    集必须经过这里取得。

    结构性收口（Task 6 评审硬要求，逐字引用）：

        ``select_promotions`` is only a DECISION function; the actual DB
        write happens when Task 12 calls
        ``civic_membership.grant_citizenship_batch``. The gate therefore
        blocks "calling ``select_promotions(mode=on)``", NOT "any write".
        Nothing structurally forces Task 12 through the gate at all. FIX
        SHAPE: give Task 12 a dedicated entry point with ``mode='on'``
        hardcoded (e.g. ``select_promotions_for_write(...)``) rather than
        letting it reuse the shadow-defaulting signature.

    ``select_promotions`` 默认 ``mode=MODE_SHADOW``（观测态，任何门槛值都放
    行）——那个默认本身就是漏洞：写路径与观测路径共用同一个签名时，调用方
    只要忘记显式传 ``mode="on"``，占位门槛就会在 off/shadow 的默认放行下
    悄悄流进 :func:`app.services.civic_membership.grant_citizenship_batch`。
    本函数不给调用方这个选择——``mode`` 在这里硬编码成 ``MODE_ON`` 且**不作
    为参数暴露**，调用方无法「忘记传」。
    """
    return select_promotions(
        snap, min_world_days=min_world_days, min_peers=min_peers,
        min_familiarity=min_familiarity, seasoning_days=seasoning_days,
        mode=MODE_ON,
    )


def promotion_evidence(
    snap: PromotionSnapshot, resident_id: str, *, min_familiarity: float,
    seasoning_days: float,
) -> dict:
    """一位候选人的判定证据（落进 ``civic_standing_history.evidence_json`` 与
    shadow 名单）。``top_familiarity`` 是对锚定公民的熟识度降序前 5——观测面
    要的是分布，不只是达标计数。"""
    anchors = anchored_citizen_ids(snap, seasoning_days=seasoning_days)
    fact = next((f for f in snap.facts if f.resident_id == resident_id), None)
    if fact is None:
        return {}
    peers = qualified_peers(snap, anchors, min_familiarity).get(
        resident_id, frozenset())
    tops = sorted(
        (fam for a, b, fam in snap.familiarity
         if (a == resident_id and b in anchors)
         or (b == resident_id and a in anchors)),
        reverse=True,
    )[:5]
    return {
        "world_days": round(
            (snap.now_world - fact.anchor_world) / timedelta(days=1), 2),
        "peers": len(peers),
        "peer_ids": sorted(peers),
        "min_familiarity": min_familiarity,
        "top_familiarity": [round(f, 4) for f in tops],
    }


# ── 快照构建（整个 pass 唯一一次 DB 读）────────────────────────────────

def _as_aware(dt: datetime) -> datetime:
    """DB 读回的 datetime 补时区。

    ``DateTime(timezone=True)`` 在 SQLite 上丢时区；本仓的存储口径是「一律
    转 UTC 落库」（``office_service._term_window`` / ``Resident.created_at``），
    所以 naive 值按 UTC 解释。
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _backfill_mark_world(db) -> datetime | None:
    """T2 完成标记（世界日期）→ tz-aware datetime；无标记/不可解析 → None。

    这是**降级路径**：主路径是「建表迁移先于 T2、T2 写历史行」，锚点直接取
    历史行。只有当某 UGC 居民一行历史都没有、而回填标记又存在时，这个值才会
    参与 ``max()``——防止运维时序反了时存量在开闸当晚被整批升回。
    """
    try:
        from app.services.config_service import ConfigService

        raw = await ConfigService(db).get(BACKFILL_MARK_KEY)
    except Exception:
        logger.debug("civic_backfill_done lookup failed", exc_info=True)
        return None
    if not raw:
        return None
    from app import world_clock

    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        logger.warning("unparseable %s=%r — 降级 anchor 路径跳过",
                       BACKFILL_MARK_KEY, raw)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=world_clock.world_epoch().tzinfo)
    return parsed


async def build_snapshot(db) -> PromotionSnapshot:
    """一次性冻结整个 pass 的输入。三次读：居民、档位历史、关系边。"""
    from sqlalchemy import select

    from app import world_clock
    from app.models.civic_standing_history import CivicStandingHistory
    from app.models.resident import Resident
    from app.models.resident_relation import ResidentRelation

    now_world = world_clock.now_world()
    residents = (await db.execute(select(Resident))).scalars().all()

    history = (await db.execute(
        select(CivicStandingHistory.resident_id,
               CivicStandingHistory.new_standing,
               CivicStandingHistory.world_at)
    )).all()
    anchor_by: dict[str, datetime] = {}
    promoted_by: dict[str, datetime] = {}
    for rid, new_standing, world_at in history:
        when = _as_aware(world_at)
        prev = anchor_by.get(rid)
        anchor_by[rid] = when if prev is None else max(prev, when)
        if new_standing == CITIZEN:
            prev_p = promoted_by.get(rid)
            promoted_by[rid] = when if prev_p is None else max(prev_p, when)

    backfill_world = await _backfill_mark_world(db)

    facts: list[ResidentFact] = []
    for r in residents:
        ugc = is_ugc_resident(r)
        anchor = anchor_by.get(r.id)
        if anchor is None:
            anchor = world_clock.real_to_world(r.created_at)
            if ugc and backfill_world is not None:
                # 降级路径（spec §7）：无历史行 + 有回填标记
                anchor = max(anchor, backfill_world)
        facts.append(ResidentFact(
            resident_id=r.id,
            slug=r.slug,
            resident_type=r.resident_type,
            is_builtin=(r.creator_id == SYSTEM_CREATOR_ID),
            is_ugc=ugc,
            anchor_world=anchor,
            promoted_world=promoted_by.get(r.id),
            banned=bool((r.meta_json or {}).get("civic_ban")),
        ))

    edges = (await db.execute(
        select(ResidentRelation.party_a, ResidentRelation.party_b,
               ResidentRelation.familiarity)
        .where(ResidentRelation.party_a_type == _RESIDENT_PARTY,
               ResidentRelation.party_b_type == _RESIDENT_PARTY)
    )).all()
    return PromotionSnapshot(
        now_world=now_world,
        facts=tuple(facts),
        familiarity=tuple((a, b, float(f or 0.0)) for a, b, f in edges),
    )


# ═══════════════════════════════════════════════════════════════════════
# 三态 pass
# ═══════════════════════════════════════════════════════════════════════
#
# **收口接线位置（本批不改 nightly_cron.py，位置在这里写死）**：
#
#     close_due_polls (nightly_cron.py:215)
#         → seed_civic_agenda (:226)
#         → maybe_open_seasonal_election (:237)
#         → 【civic_promotion 接在这里，≈:245】
#         → run_npc_voting (:247)
#         → office term_check (:263)
#
# 理由是语义决策：当晚晋升、当晚补投，新公民参与的第一次关票分子分母同源。
# 接在末尾并不能消除危害，只把它推迟一晚——每晚 close(215) 先于 vote(247)，
# 夜 N 末尾晋升的人在夜 N+1 关票时仍然是「进了分母、一票未投」。收口接线时
# 用与 nightly_cron.py:142-145（opinion drift 顺序硬约束）同样的注释形式锚住
# 位置，对应回归测试按 **N+1 晚** 断言。

PROMOTION_ACTOR = "civic_promotion"
PROMOTION_REASON_CODE = "threshold_met"
PROMOTION_REASON = "满足公民权晋升门槛（在镇世界日 + 与锚定公民的熟识度）"

#: 每次运行的摘要落点。shadow 态不产生历史行，探针只能从这里读候选名单。
#: ``SystemConfig.value`` 是 ``String(2000)``，所以名单截断到 50 个 slug。
RUN_SUMMARY_KEY = "civic_promotion_last_run"
_SUMMARY_MAX_SLUGS = 50


async def _player_avatar_ids(db, resident_ids) -> frozenset[str]:
    """候选集里有哪些其实是玩家化身——``users.player_resident_id`` 命中。

    候选面纪律：玩家化身永远不是候选人。写入口
    （:func:`app.services.civic_membership.grant_citizenship_batch`）查同一张
    表整批拒绝是防线在生效，但让它端着一个必被拒绝的候选人走到写入口，会让
    同一批里其它合法候选跟着永久卡死——``CivicStandingRefused`` 是整批拒绝，
    不分谁的锅。候选面必须在自己的防线上先把它筛掉，不能指望写入口的射程
    检查兜底。

    复现路径：admin 手滑把化身的 ``resident_type`` 改成 denizen 档，若
    ``meta_json`` 没有 ``origin`` 键，``is_ugc_resident`` 第 5 条兜底
    （``creator_id is not None``）会把它判成 UGC，候选面因此收它。这条兜底
    本身是否该收窄，是收口时才拍板的产品决策（见
    ``civic_membership.is_ugc_resident`` 的 docstring），本函数不碰它，只是
    在候选面这一层加一道独立防线。
    """
    if not resident_ids:
        return frozenset()
    from sqlalchemy import select

    from app.models.user import User

    hits = (await db.execute(
        select(User.player_resident_id)
        .where(User.player_resident_id.in_(resident_ids))
    )).scalars().all()
    return frozenset(hits)


async def _record_run(db, result: dict) -> None:
    """把本次运行摘要写进 ``system_config``（fail-open）。

    这是 shadow 态**唯一**的一次写——政治层（``residents`` /
    ``civic_standing_history``）零写入。

    用**独立于调用方 ``db`` 的一次性会话**写、提交、关闭（复审 Important 2）
    ——不能借用 ``db`` 自己的事务边界。``ConfigService.set`` 末尾是
    ``await self._db.commit()``，而 ``Session.commit()`` 不受 ``begin_nested()``
    savepoint 约束：它总是提交到最外层，savepoint 只能 release/rollback 自己
    那一段，管不住 ``commit()``（用两段实验脚本验证过，见任务报告）。
    ``run_promotion_pass`` 对调用方传入什么样的 ``db`` 没有任何契约保证——如
    果 ``db`` 上还有别的未提交改动（比如接进 nightly_cron 后与其它任务共用
    同一个 session），直接在 ``db`` 上 commit 会把那些改动一并带下去，与
    「shadow 对 residents / civic_standing_history 零写入」的承诺打架。

    ``AsyncEngine(db.get_bind())`` 包住的是与 ``db`` 同一个底层 sync engine
    （同一个连接池、同一个物理数据库）——不是重新 ``create_async_engine``，
    那样在 SQLite ``:memory:`` 下会开出一个全新的空库，测试会读不到自己刚
    写的东西；``AsyncSession.get_bind()`` 返回的本来就是这个 sync engine
    （不是 ``AsyncEngine``），``AsyncEngine(...)`` 只是把它重新包一层给
    ``async_sessionmaker`` 用，不建新连接池。
    """
    try:
        from sqlalchemy.ext.asyncio import (
            AsyncEngine, AsyncSession, async_sessionmaker,
        )

        from app.services.config_service import ConfigService

        payload = {
            "mode": result.get("mode"),
            "world_at": result.get("world_at"),
            "citizens_before": result.get("citizens_before"),
            "candidates": list(result.get("candidates") or [])[:_SUMMARY_MAX_SLUGS],
            "candidate_count": len(result.get("candidates") or []),
            "promoted": result.get("promoted", 0),
            "refused": result.get("refused"),
            "refused_detail": result.get("refused_detail"),
        }
        scratch_factory = async_sessionmaker(
            AsyncEngine(db.get_bind()), class_=AsyncSession,
            expire_on_commit=False)
        async with scratch_factory() as scratch:
            await ConfigService(scratch).set(
                RUN_SUMMARY_KEY, payload, group="civic",
                updated_by=PROMOTION_ACTOR,
            )
    except Exception:
        logger.warning("recording civic_promotion run summary failed",
                       exc_info=True)


async def run_promotion_pass(db) -> dict:
    """一夜一次的晋升 pass。返回运行摘要（也是探针与测试的读数来源）。

    三态（``CIVIC_PROMOTION_MODE``）：

    - ``off``（默认）：零读零写立即返回，行为与本批开工前逐字节一致；
    - ``shadow``：完整候选计算 + **全部防呆检查**，名单与证据进日志与运行
      摘要，**对 residents / civic_standing_history 零写入**。生产至少观察 3 个
      夜间周期，名单规模与标定预期一致才进开闸。首夜爆炸半径不可预演，
      shadow 是带全部防呆的实跑演练 + 名单落盘；
    - ``on``：真正执行 :func:`app.services.civic_membership.grant_citizenship_batch`。

    数值闸门的顺序：**先用完整候选集判熔断，再按单夜上限确定性截断**。反过来
    熔断永远打不响（截断后的集合恒 ≤ 上限）。

    熔断阈值是 ``max(绝对下限, 公民数 × 比例)``，两项缺一不可：只有比例项时，
    小镇规模下熔断恒响（11 位公民 × 0.20 ≈ 2.2），单夜上限默认 5 永远够不着，
    闸门 1 变成死代码；只有下限时，世界长大后熔断就不再随规模缩放。

    写路径的结构性收口（Task 6 评审硬要求）：``mode == MODE_ON`` 时，候选集
    **只能**经 :func:`select_promotions_for_write` 取得——该函数把 mode="on"
    硬编码、不对外暴露 mode 形参，调用方没有「忘记传 mode='on'」这个选项，
    也就没有绕过 :func:`assert_thresholds_calibrated` 直接把占位门槛写进库
    的路。``off``/``shadow`` 两态永远不会触达这个函数：``off`` 在此之前已经
    return，``shadow`` 分支用的是 :func:`select_promotions` 的默认（观测态）
    签名。

    候选面纪律（复审 Important 1）：候选集算出来之后、任何写之前，会先剔除
    玩家化身（:func:`_player_avatar_ids`）——写入口的射程检查拒绝它是防线在
    生效，但候选面把它端上来会连累同一批里的合法候选一起被整批拒绝。

    拒绝容错（复审 Important 1）：``grant_citizenship_batch`` 抛出的
    ``CivicStandingRefused``（射程防呆之外的任何拒绝类，比如并发窗口内被
    改过 ``resident_type``）在这里被捕获，不会传给调用方——``nightly_cron``
    里一炸会中断整条夜间链。拒绝路径也会调用 :func:`_record_run`，运行摘要
    带上拒绝原因，不会因为异常发生在 ``_record_run`` 之前而静默消失。
    """
    from app.services.civic_membership import (
        CivicStandingRefused, auto_demotion_enabled, grant_citizenship_batch,
        min_familiarity, min_peers, min_world_days, peer_seasoning_world_days,
        promotion_breaker_fraction, promotion_breaker_min_abs,
        promotion_max_per_run, promotion_mode,
    )

    mode = promotion_mode()
    if mode not in (MODE_OFF, MODE_SHADOW, MODE_ON):
        logger.error("unknown CIVIC_PROMOTION_MODE=%r — 按 off 处理", mode)
        mode = MODE_OFF
    if mode == MODE_OFF:
        return {"mode": MODE_OFF, "world_at": None, "citizens_before": None,
                "candidates": [], "evidence": {}, "promoted": 0,
                "demoted": 0, "refused": None}

    if auto_demotion_enabled():
        raise NotImplementedError(
            "CIVIC_AUTO_DEMOTION_ENABLED=true，但自动下滑降级 v1 未实现。开启"
            "前必须先落地滞后三件套：滞后区间 Δ ≥ 0.10（严格大于单次最大相关"
            "增量 0.05）、最短任期 ≥ 12 世界日（= 一张 poll 的生命周期）、冷却"
            "期 ≥ 12 世界日。缺一不可——门槛②读的 familiarity 有周衰减，没有"
            "滞后就是让公民权跟着社交波动飘。"
        )

    seasoning = peer_seasoning_world_days()
    threshold = min_familiarity()
    snap = await build_snapshot(db)

    # 写路径的结构性收口：mode == MODE_ON 的候选集只能经
    # select_promotions_for_write 取得（见本函数 docstring）；shadow 用
    # select_promotions() 的默认（观测态）签名——观测态必须能在占位门槛上跑，
    # 这里不能触发标定闸门。判定本身与 mode 无关，同一组入参在两态下选出
    # 同一批人，唯一的区别是「是否会在占位门槛上 raise」。
    if mode == MODE_ON:
        candidate_ids = select_promotions_for_write(
            snap, min_world_days=min_world_days(), min_peers=min_peers(),
            min_familiarity=threshold, seasoning_days=seasoning,
        )
    else:
        candidate_ids = select_promotions(
            snap, min_world_days=min_world_days(), min_peers=min_peers(),
            min_familiarity=threshold, seasoning_days=seasoning,
        )

    slug_by_id = {f.resident_id: f.slug for f in snap.facts}

    # 候选面纪律：玩家化身永远不是候选人（见本函数 docstring）。剔除放在
    # 这里——breaker/cap 两道数值闸门看到的都是筛过的集合，一个必被写入口
    # 拒绝的候选人不该占熔断/上限的名额。
    avatar_ids = await _player_avatar_ids(db, candidate_ids)
    if avatar_ids:
        logger.warning(
            "civic_promotion: %d candidate(s) are player avatars — excluded "
            "from the pass (the write entry point would refuse the whole "
            "batch otherwise): %s",
            len(avatar_ids),
            sorted(slug_by_id.get(a, a) for a in avatar_ids))
        candidate_ids = tuple(c for c in candidate_ids if c not in avatar_ids)

    citizens_before = sum(1 for f in snap.facts
                          if f.resident_type in CIVIC_VOTER_TYPES)
    result = {
        "mode": mode,
        "world_at": snap.now_world.isoformat(),
        "citizens_before": citizens_before,
        "candidates": [slug_by_id.get(i, i) for i in candidate_ids],
        "evidence": {},
        "promoted": 0,
        "demoted": 0,          # 夜间任务只升，永不自动降
        "refused": None,
    }

    # 闸门 2（熔断）：用**完整**候选集判，整批拒绝、不截断。
    # 阈值 = max(绝对下限, 公民数 × 比例)——绝对下限保证小批量能放行（否则
    # 4 位内置公民 × 0.20 = 0.8，一个候选都过不去），比例保证世界长大后熔断
    # 仍随规模缩放。
    breaker = promotion_breaker_fraction()
    breaker_min_abs = promotion_breaker_min_abs()
    breaker_limit = max(float(breaker_min_abs), citizens_before * breaker)
    if candidate_ids and len(candidate_ids) > breaker_limit:
        logger.error(
            "civic_promotion circuit breaker: %d candidate(s) > limit %.2f = "
            "max(min_abs=%d, %d citizens × %.2f) — 整批拒绝（截断会掩盖"
            "「阈值写反」这类全量误判）。名单：%s",
            len(candidate_ids), breaker_limit, breaker_min_abs,
            citizens_before, breaker, result["candidates"])
        result["refused"] = "circuit_breaker"
        await _record_run(db, result)
        return result

    # 闸门 1（单夜上限）：确定性截断（candidate_ids 已按 id 排序），余量下夜再来
    cap = promotion_max_per_run()
    picked = candidate_ids[:cap]
    if len(picked) < len(candidate_ids):
        logger.warning(
            "civic_promotion per-run cap: promoting %d of %d candidate(s) "
            "tonight (CIVIC_PROMOTION_MAX_PER_RUN=%d); 余量下夜再来",
            len(picked), len(candidate_ids), cap)

    # 只为真正会被处理（写库或 shadow 展示）的这一批算证据——不为被单夜上限
    # 截掉的溢出候选白算一遍 promotion_evidence（复审「折进来」的清理项）。
    evidence_by_id = {
        i: promotion_evidence(snap, i, min_familiarity=threshold,
                              seasoning_days=seasoning)
        for i in picked
    }
    result["evidence"] = {slug_by_id.get(i, i): evidence_by_id[i]
                          for i in picked}

    if mode == MODE_SHADOW:
        logger.info(
            "civic_promotion SHADOW: %d candidate(s) would be promoted "
            "tonight — %s | evidence=%s",
            len(picked), [slug_by_id.get(i, i) for i in picked],
            result["evidence"])
        await _record_run(db, result)
        return result

    if picked:
        try:
            result["promoted"] = await grant_citizenship_batch(
                db, list(picked), reason=PROMOTION_REASON,
                reason_code=PROMOTION_REASON_CODE, actor=PROMOTION_ACTOR,
                evidence_by_id=evidence_by_id,
            )
        except CivicStandingRefused as exc:
            # 拒绝容错：不把异常传给调用方——nightly_cron 里一炸会中断整条
            # 夜间链。run summary 也要走这条路径写，不能因为异常发生在
            # _record_run 之前而让本次运行静默消失、探针上看不到任何线索。
            logger.error(
                "civic_promotion grant refused: %s — 整批拒绝，未晋升；"
                "本轮尝试：%s", exc, result["candidates"])
            result["refused"] = "grant_refused"
            result["refused_detail"] = str(exc)
            await _record_run(db, result)
            return result
    await _record_run(db, result)
    logger.info("civic_promotion pass done: mode=%s candidates=%d promoted=%d",
                mode, len(candidate_ids), result["promoted"])
    return result
