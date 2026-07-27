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
