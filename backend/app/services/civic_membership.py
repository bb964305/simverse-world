"""Resident-type membership sets — the town's two *different* boundaries.

``resident_type`` is a bare ``String(20)`` (``app/models/resident.py:53``, no
enum / no CHECK). Ten reads compare against it, and those ten are **not** one
semantic family: the moment player-authored (UGC) residents stop being typed
``"npc"``, the political reads must narrow while the population reads must not.

``Resident.is_autonomous`` already collapsed all ten onto a single predicate,
which is precisely the latent regression this module exists to split back
apart. The hybrid keeps its name and its population meaning; a second hybrid,
``Resident.is_civic_voter``, carries the political one.

``CIVIC_VOTER_TYPES``
    Political rights — who may cast a civic vote, who counts in the quorum
    denominator, who may stand for mayor. Narrow by default; adding a type here
    hands that type the ballot.

``SIM_RESIDENT_TYPES``
    World population — who is a simulated inhabitant at all: is ticked by the
    agent loop, shows on the town-hall roster, can hold a duty (labour, not
    politics), is swept by mayor-meta maintenance, joins the lecture debate
    pool. Broad by default; removing a type here makes that type silently
    vanish from the simulation.

The two sets are deliberately *not* nested by construction, but today
``CIVIC_VOTER_TYPES ⊂ SIM_RESIDENT_TYPES`` holds: a voter is always an
inhabitant.

Values (all live literals, no migration):

- ``"npc"``      built-in autonomous cast (``seed/preset_characters.py``,
                 ``origin="preset"``) — has political rights
- ``"resident"`` player-authored resident via forge / import — inhabitant,
                 **no** political rights
- ``"player"``   the user's own single avatar (``onboarding_service.py``) —
                 deliberately absent from *both* sets, and governed by a third,
                 untouched predicate family (``!= "player"``)
- ``"preset"``   admin-created resident (``schemas/admin.py:129`` default) —
                 absent from both sets, matching pre-hotfix behaviour exactly
"""

#: A-class reads (3): political rights. Widening this hands out the ballot.
CIVIC_VOTER_TYPES = frozenset({"npc"})

#: B/C-class reads (10): world population & ops sweeps. Narrowing this makes
#: residents silently disappear from the simulation.
#:
#: ``"resident"`` must be added here in the *same commit* that starts writing
#: it at the creation paths — an intermediate state where UGC residents are
#: typed ``"resident"`` but the population set still says ``{"npc"}`` would
#: erase them from the agent loop, the town-hall roster, the duty lookup and
#: the mayor sweeps.
SIM_RESIDENT_TYPES = frozenset({"npc", "resident"})

#: The type given to player-authored (forge / import) residents. Satisfies the
#: untouched ``!= "player"`` predicate family by construction, so world
#: presence (map, home-decor ownership, purge-candidacy) is unchanged.
UGC_RESIDENT_TYPE = "resident"


# ═══════════════════════════════════════════════════════════════════════
# F2 —— 出身（provenance）× 档位（standing）二维模型
# ═══════════════════════════════════════════════════════════════════════
#
# 维度 A · 出身：``resident_type`` 的四个取值今天各有唯一创建路径，本轮之后
# 除 admin 纠错外不再被业务改写（见 ``is_ugc_resident``）。
#
# 维度 B · 档位：有序三档，正好对应「降级与逐出是同一套机制的不同强度」::
#
#     citizen  有票 · 在镇 · 被 loop 驱动          ← 晋升终点
#     denizen  无票 · 在镇 · 被 loop 驱动          ← 降级落点（本轮实现）
#     exiled   无票 · 不在镇 · 不被驱动 · 不在地图  ← 逐出落点（本轮仅预留）
#
# v1 的落地形态是**零列变更**：档位仍由 ``resident_type`` 的 npc / resident
# 编码，但任何业务代码不得再直接读写该列，一律走本模块的派生函数与两个写
# 入口 ``grant_citizenship`` / ``revoke_citizenship``。
#
# **不新增第 5 个取值（例如 "exiled"）**：地图与感知不读 type——公开名录是
# 全表（``app/services/resident_service.py:6-18``），tile 占用也是全表
# （``app/services/resident_placement.py:104-111`` / ``:157-160``）。新增取值
# 只会掉出 ``SIM_RESIDENT_TYPES``，产出「仍在地图上、仍被搭话，只是自己不再
# tick」的活体雕像。逐出要收窄的是第四族谓词 ``is_in_town``（v1 不实现，语义
# 已在此写死：出现在公开名录/地图 + 占用住房 + 占用 tile，三处口径统一开关）。
#
# 本模块被 ``app/models/resident.py:8`` 在**模型层**导入，所以顶层只许 stdlib
# 与 sqlalchemy；``app.models.*`` / ``app.config`` 一律惰性导入。

import logging
import os

logger = logging.getLogger(__name__)

CITIZEN = "citizen"
DENIZEN = "denizen"
EXILED = "exiled"

#: 有序三档（强度递增的撤销落点）。
CIVIC_STANDINGS: tuple[str, str, str] = (CITIZEN, DENIZEN, EXILED)

#: citizen 档在 v1 编码成的 ``resident_type``。**禁止任务里出现裸字面量**
#: ``"npc"``——该列是裸 ``String(20)``、无 enum 无 CHECK
#: （``app/models/resident.py:55``），写错一个字符（``"npc "``）就同时掉出
#: ``CIVIC_VOTER_TYPES`` 与 ``SIM_RESIDENT_TYPES``，居民从 agent loop、市政厅
#: 名册、职务查找、mayor 清扫里一起消失。
CIVIC_MEMBER_TYPE = "npc"

#: 玩家化身（``users.player_resident_id`` 的单值 FK）。刻意不在任何档位里，
#: 由第三族谓词 ``!= "player"`` 管辖。
PLAYER_RESIDENT_TYPE = "player"

#: admin 创建的 resident（``app/schemas/admin.py:129`` 默认值）。两个集合之外，
#: 本轮不动（U6 待决项）。
ADMIN_PRESET_TYPE = "preset"

#: ``app/routers/admin/residents.py`` 给 admin preset 写的 creator_id 字面量。
ADMIN_PRESET_CREATOR_ID = "system"

#: 内置阵容的 creator_id。与 ``seed/preset_characters.py:20`` 的
#: ``SYSTEM_USER_ID`` 逐字相等（由 tests 断言）。**刻意重复字面量而不是
#: import**：``app/`` 全仓今天零处 ``import seed``，而本模块被模型层导入，
#: 把 1200 行的种子数据模块拉进来会把层次倒过来。
SYSTEM_CREATOR_ID = "00000000-0000-0000-0000-000000000001"

#: UGC 创建路径写进 ``meta_json['origin']`` 的三个取值（五处创建点：
#: ``app/forge/pipeline.py::ForgePipeline``、``app/forge/legacy_pipeline.py``
#: 的 forge 与 quick_forge 两处、``app/routers/residents.py`` 的 import 两处）。
#: 这里刻意只写路径 + 符号名不写行号——行号会随代码漂移成陈旧文档。
#: T2 存量回填脚本与 F2 夜间任务**共用本模块的判定**，两边各写一份必然漂移。
UGC_ORIGINS = frozenset({"forge", "import", "quick_forge"})

#: 明确的**非** UGC 出身。``"preset"`` 是内置阵容与 admin preset 的共同出身；
#: ``"onboarding"`` 是玩家化身的出身（``app/services/onboarding_service.py``
#: 的 ``Resident(resident_type="player", meta_json={"origin": "onboarding"})``）。
#:
#: ``"onboarding"`` 必须在这里，而不能只靠 ``resident_type == "player"`` 挡：
#: admin 手滑把化身的 type 改成 ``resident`` 之后 type 已不可信，而化身的
#: ``creator_id`` 是真实 user id，:func:`is_ugc_resident` 的兜底分支
#: （``return creator_id is not None``）会把它判成 UGC → 进晋升候选面 → 被夜间
#: 任务自动授予投票权；此后 :func:`_assert_revocable` 的玩家化身 FK 复核又会
#: 拒绝撤销，人就永久卡在 citizen 档。
NON_UGC_ORIGINS = frozenset({"preset", "onboarding"})

#: 民选职务的 ``offices.fill_strategy``（迁移 046 只给 mayor 写了这个值）。
#: 撤销只卸民选职务——``town_clerk`` / ``postman`` / ``doctor`` 是**劳动职务**，
#: offices 表把两类混在一张表里，一刀切会误伤。
POLITICAL_FILL_STRATEGY = "election"

#: 档位 → ``resident_type``。``EXILED`` 刻意缺席，见 :func:`standing_to_type`。
STANDING_TO_TYPE: dict[str, str] = {
    CITIZEN: CIVIC_MEMBER_TYPE,
    DENIZEN: UGC_RESIDENT_TYPE,
}
TYPE_TO_STANDING: dict[str, str] = {v: k for k, v in STANDING_TO_TYPE.items()}


class CivicStandingRefused(RuntimeError):
    """一次档位变更被防呆拒绝。

    对标 ``seed/reset_builtin_residents.py:60`` 的 ``PlayerPurgeRefused``，
    照抄它的两条设计选择：

    - **Raise，不 skip**——静默跳过会让调用方以为动作完成了；
    - **读数据库，不信传入对象**——调用点自己建的目标列表里，
      ``target.resident_type`` 恰恰是不能信的字段。

    永远在**第一条 UPDATE 之前**抛出（"Guard first: no UPDATE has run yet"），
    使拒绝是真正的 no-op。
    """


# ── 档位派生函数 ───────────────────────────────────────────────────────

def civic_standing(resident) -> str:
    """该居民当前的档位，取值来自 :data:`CIVIC_STANDINGS`。

    ``"exiled"`` 现在就在枚举里，但 v1 没有任何 ``resident_type`` 取值映射到
    它——逐出上线时是在 :func:`standing_to_type` 填空，不是改签名。

    ``player`` / ``preset`` / 写错的字面量都会 ``ValueError``：把它们当成某个
    档位悄悄处理，正是本模块存在的理由的反面。调用方（两个写入口）在调用本
    函数之前已经做完射程防呆，所以不会拿玩家化身来问。
    """
    rtype = getattr(resident, "resident_type", None)
    standing = TYPE_TO_STANDING.get(rtype)
    if standing is None:
        raise ValueError(
            f"resident_type {rtype!r} 不在档位模型内："
            f"{PLAYER_RESIDENT_TYPE!r} 由第三族谓词（!= \"player\"）管辖，"
            f"{ADMIN_PRESET_TYPE!r} 是两个集合之外的待决项，其余取值是写错的"
            f"字面量（该列无 enum 无 CHECK）。已知映射：{TYPE_TO_STANDING}"
        )
    return standing


def standing_to_type(standing: str) -> str:
    """档位 → v1 的 ``resident_type`` 编码。"""
    if standing == EXILED:
        raise NotImplementedError(
            "exile 档 v1 不实现：档位枚举、revoke_citizenship(tier='exile') 与"
            "分档清理表都已按两档写好，落地时在这里补 is_in_town 的收窄"
            "（公开名录 / tile 占用 / 住房三处口径），不需要改任何签名。"
        )
    try:
        return STANDING_TO_TYPE[standing]
    except KeyError:
        raise ValueError(
            f"unknown civic standing {standing!r}; expected one of "
            f"{CIVIC_STANDINGS}"
        ) from None


def assert_known_types(*types: str) -> None:
    """取值白名单断言（数值闸门 4）。

    ``new_type`` 与 ``expected_type`` 都必须取自本模块导出的常量且落在
    ``SIM_RESIDENT_TYPES`` 里，不满足直接拒绝——这是「写错一个字符就让居民从
    整个模拟里消失」的唯一兜底。
    """
    unknown = [t for t in types if t not in SIM_RESIDENT_TYPES]
    if unknown:
        raise CivicStandingRefused(
            f"refusing a standing transition with resident_type value(s) "
            f"{unknown!r}: not in SIM_RESIDENT_TYPES={sorted(SIM_RESIDENT_TYPES)}"
        )


# ── UGC（出身）判定：T2 脚本与 F2 任务的唯一来源 ───────────────────────

def is_ugc_resident(resident) -> bool:
    """这个居民是不是玩家创作的（UGC）。

    判定优先级（``creator_id`` 是三值混合，不能单条判定）：

    1. 玩家化身 → False（第三族谓词管辖）；
    2. ``creator_id == SYSTEM_CREATOR_ID`` → False（内置阵容）；
    3. ``meta_json['origin'] in UGC_ORIGINS`` → True（forge / import 五处）；
    4. ``meta_json['origin'] in NON_UGC_ORIGINS`` → False。``"preset"`` 是
       **内置阵容与 admin 创建的 preset 的共同出身**，``"onboarding"`` 是玩家
       化身的出身——所以 origin 只是辅助信号，provenance 主键是 creator_id；
    5. 其余：有非空 ``creator_id`` 即算 UGC（极老的 UGC 行不保证带 origin）。

    第 4 条里的 ``"onboarding"`` 是**射程纪律**，不是装饰：第 1 条只在 type 还
    可信时有效，而 admin 手滑把化身改成 ``resident`` 之后 type 恰恰不可信，那
    时化身的真实 ``creator_id`` 会让第 5 条把它判成 UGC。

    ⚠️ 账号注销后 ``creator_id`` 变 NULL（迁移 045）且无 origin 的行判 False——
    保守，由 T2 的「残差人工点名复核」兜底。宁可漏升，不可误降。
    """
    rtype = getattr(resident, "resident_type", None)
    if rtype == PLAYER_RESIDENT_TYPE:
        return False
    creator_id = getattr(resident, "creator_id", None)
    if creator_id == SYSTEM_CREATOR_ID:
        return False
    origin = (getattr(resident, "meta_json", None) or {}).get("origin")
    if origin in UGC_ORIGINS:
        return True
    if origin in NON_UGC_ORIGINS:
        return False
    if creator_id == ADMIN_PRESET_CREATOR_ID:
        return False
    return creator_id is not None


def ugc_filter():
    """UGC 的 **SQL 粗筛**谓词（返回一个 SQLAlchemy 布尔表达式）。

    ``meta_json`` 是 ``sa.JSON`` 而非 jsonb，跨 sqlite / PostgreSQL 没有可移植
    的 JSON 路径查询，所以 SQL 只能按 ``resident_type`` + ``creator_id`` 粗筛。
    **粗筛保证是超集**，精确判定必须再过一遍 :func:`is_ugc_resident`。

    三值 NULL 陷阱：``creator_id != :x`` 在 ``creator_id IS NULL`` 时求值为
    NULL（行被丢掉），所以必须显式 ``OR creator_id IS NULL``。
    """
    from sqlalchemy import and_, or_

    from app.models.resident import Resident   # 惰性：模型层导入本模块

    return and_(
        Resident.resident_type != PLAYER_RESIDENT_TYPE,
        Resident.resident_type != ADMIN_PRESET_TYPE,
        or_(
            Resident.creator_id.is_(None),
            and_(
                Resident.creator_id != SYSTEM_CREATOR_ID,
                Resident.creator_id != ADMIN_PRESET_CREATOR_ID,
            ),
        ),
    )


# ── 运行时旋钮 ─────────────────────────────────────────────────────────
#
# 本批**不改** ``app/config.py`` / ``.env.example``（收口 §8 统一补齐），所以
# 旋钮走 env + 模块内 fallback。形状照抄 ``app/services/social_status_recovery
# .py:57-67``：env 是运行时来源，``Settings`` 未来接管 fallback，收口给
# ``Settings`` 加同名字段后本文件零改动即可生效。
#
# ⚠️ 三个门槛值（MIN_WORLD_DAYS / MIN_PEERS / MIN_FAMILIARITY）这里给的是
# **占位默认值，标定前不得开闸**——真实取值必须由生产分布反推（使晋升面非空
# 且非全量）。``rep_credit_min_score = -0.3`` 之所以变成装饰性闸门，正是因为
# 它是拍出来的。

_TRUE = {"1", "true", "yes", "on"}

#: 三个门槛的占位默认值（待生产数据复标）
_DEFAULT_MIN_WORLD_DAYS = 30.0
_DEFAULT_MIN_PEERS = 3
#: 刻意避开 ``realism_circle_threshold = 0.3``（``app/config.py:512``，圈子
#: 检测的强边阈值），撞上去会让两套语义纠缠。
_DEFAULT_MIN_FAMILIARITY = 0.20
#: 归化公民进入「锚定公民集」前的考察期
_DEFAULT_PEER_SEASONING_WORLD_DAYS = 28.0
#: 一张 poll 开 ``civic_poll_days = 3`` 真实天 = 12 世界日（k=4）。最短任期与
#: 冷却期的**下限**就是它：更小则单张 poll 生命周期内公民权仍可翻转。
_MIN_HYSTERESIS_WORLD_DAYS = 12.0
#: ``open_election`` 需要 ≥2 候选（``app/services/election_service.py:62-63``）
_ABSOLUTE_MIN_ELECTORATE = 3
#: 熔断的**绝对下限**（见 :func:`promotion_breaker_min_abs`）。生产内置阵容
#: ≈10-11 位公民，只按比例算的阈值 ≈2.2，一夜 3 个合法候选就整批拒绝，而单夜
#: 上限默认 5 永远够不着——两道闸门互相吞掉。
_DEFAULT_BREAKER_MIN_ABS = 3


def _settings_default(name: str, default):
    """env 旋钮的注册默认值：收口把同名字段加进 ``Settings`` 后自动生效。"""
    try:
        from app.config import settings   # 惰性：避免与模型层构成循环导入

        return getattr(settings, name.lower(), default)
    except Exception:      # config 导入失败绝不能打断政治层判定
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return str(_settings_default(name, default))
    return raw.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return bool(_settings_default(name, default))
    return raw.strip().lower() in _TRUE


def _env_float(name: str, default: float) -> float:
    fallback = float(_settings_default(name, default))
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return fallback
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r — using %s", name, raw, fallback)
        return fallback


def _env_int(name: str, default: int) -> int:
    fallback = int(_settings_default(name, default))
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return fallback
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid %s=%r — using %s", name, raw, fallback)
        return fallback


def promotion_mode() -> str:
    """``off`` | ``shadow`` | ``on``。默认 ``off``——off 时行为与本批开工前
    逐字节一致。"""
    return _env_str("CIVIC_PROMOTION_MODE", "off").lower()


def min_world_days() -> float:
    """门槛①：在镇**世界日**（不是真实日）。占位值，待生产分布复标。"""
    return _env_float("CIVIC_PROMOTION_MIN_WORLD_DAYS", _DEFAULT_MIN_WORLD_DAYS)


def min_peers() -> int:
    """门槛②：达标的锚定公民同伴数下限。占位值，待生产分布复标。"""
    return _env_int("CIVIC_PROMOTION_MIN_PEERS", _DEFAULT_MIN_PEERS)


def min_familiarity() -> float:
    """门槛② 的 θ。占位值，待生产分布复标；不得取 0.3。"""
    return _env_float("CIVIC_PROMOTION_MIN_FAMILIARITY", _DEFAULT_MIN_FAMILIARITY)


def peer_seasoning_world_days() -> float:
    """归化公民成为「锚定公民」前的考察期（世界日）。"""
    return _env_float("CIVIC_PEER_SEASONING_WORLD_DAYS",
                      _DEFAULT_PEER_SEASONING_WORLD_DAYS)


def promotion_max_per_run() -> int:
    """数值闸门 1：单夜移动分母的上限（超出按确定性顺序截断，余量下夜再来）。"""
    return max(0, _env_int("CIVIC_PROMOTION_MAX_PER_RUN", 5))


def promotion_breaker_fraction() -> float:
    """数值闸门 2 的比例项：候选集 > ``max(绝对下限, 当前公民数 × 该比例)``
    → **整批拒绝并告警，不截断**。截断会掩盖「阈值写反」这类全量误判。"""
    return _env_float("CIVIC_PROMOTION_BREAKER_FRACTION", 0.20)


def promotion_breaker_min_abs() -> int:
    """数值闸门 2 的**绝对下限**：熔断阈值取
    ``max(promotion_breaker_min_abs(), citizens × promotion_breaker_fraction())``。

    没有这个下限，小镇规模下熔断恒响、单夜上限恒不生效：生产内置阵容
    ≈10-11 位公民 × 0.20 ≈ 2.2，一夜 3 个合法候选就整批拒绝，而
    ``CIVIC_PROMOTION_MAX_PER_RUN`` 默认 5 永远够不着——闸门 1 在真实世界里
    是死代码，两道闸门的语义互相吞掉。

    下限本身也是可调的：置 0 即退化成纯比例判定（世界规模足够大之后）。
    """
    return max(0, _env_int("CIVIC_PROMOTION_BREAKER_MIN_ABS",
                           _DEFAULT_BREAKER_MIN_ABS))


def min_electorate() -> int:
    """数值闸门 3 的下限之一。低于 3 时撤销可以把选举机制打死。"""
    return max(_ABSOLUTE_MIN_ELECTORATE,
               _env_int("CIVIC_MIN_ELECTORATE", _ABSOLUTE_MIN_ELECTORATE))


def min_tenure_world_days() -> float:
    """晋升后此期内不得降级（世界日）。v1 只用于探针观测，自动降级未实现。"""
    return max(_MIN_HYSTERESIS_WORLD_DAYS,
               _env_float("CIVIC_MIN_TENURE_WORLD_DAYS",
                          _MIN_HYSTERESIS_WORLD_DAYS))


def promotion_cooldown_world_days() -> float:
    """降级后此期内不得复升（世界日）。v1 只用于探针观测。"""
    return max(_MIN_HYSTERESIS_WORLD_DAYS,
               _env_float("CIVIC_PROMOTION_COOLDOWN_WORLD_DAYS",
                          _MIN_HYSTERESIS_WORLD_DAYS))


def auto_demotion_enabled() -> bool:
    """自动下滑降级总开关，默认关。

    开启必须**同时**具备滞后三件套（缺一不可）：滞后区间 Δ ≥ 0.10（严格大于
    单次最大相关增量 0.05——聊天 ``realism_rel_familiarity_chat``、arc 完结
    ``arc_service.py:213``）、最短任期 ≥ 12 世界日、冷却期 ≥ 12 世界日。三件套
    未实现，所以 :func:`app.tasks.civic_promotion.run_promotion_pass` 在这个
    开关为真时直接 ``raise NotImplementedError``。

    注意：衰减用的是**真实日**（``realism_rel_decay_idle_days = 30``）而门槛用
    **世界日**——这是有意的两套尺度，实现不得擅自统一。
    """
    return _env_bool("CIVIC_AUTO_DEMOTION_ENABLED", False)


# ═══════════════════════════════════════════════════════════════════════
# 写入口 ①：晋升
# ═══════════════════════════════════════════════════════════════════════
#
# ``resident_type`` 在本轮之后只许由本模块的两个写入口（加 admin 路由的转调）
# 改写。列上没有 CHECK，代码就是最后一道闸——``tests/
# test_civic_standing_write_entrypoints.py`` 用 AST 扫描把这条钉住。


async def _write_history(
    db, *, resident_id: str, old_standing: str, new_standing: str,
    reason: str | None, reason_code: str, actor: str,
    evidence: dict | None,
) -> None:
    """落一行 ``civic_standing_history``（可回滚硬门 + 公民时钟锚点）。

    不 commit——由调用方决定事务边界。``world_at`` 存 UTC-aware：
    ``DateTime(timezone=True)`` 在 SQLite 上丢时区，统一转 UTC 存、读回补 UTC
    才能无损往返。
    """
    from datetime import UTC

    from app import world_clock
    from app.models.civic_standing_history import CivicStandingHistory

    db.add(CivicStandingHistory(
        resident_id=resident_id,
        old_standing=old_standing,
        new_standing=new_standing,
        reason=reason,
        reason_code=reason_code,
        actor=actor,
        evidence_json=evidence or {},
        world_at=world_clock.now_world().astimezone(UTC),
    ))


async def _emit_standing_changed(
    db, *, slug: str, old_standing: str, new_standing: str, reason_code: str,
) -> None:
    """广播 ``civic_standing_changed``（world_changed v1 信封，fail-open）。

    ⚠️ 事件名**不得**叫 ``resident_type_changed``——该名字已被 SBTI 人格类型
    漂移占用（``app/ws/handlers/chat.py:474-482``），复用会让前端把政治事件
    渲染成人格变化。payload 只带 ``reason_code`` 枚举码，**永不带 reason
    文本**。挂 world_revision / seq 的写法参照
    ``app/services/office_service.py:244-271``；注意那是易失的 WS 扇出、
    **不落任何表**，不能拿它当「可回滚」硬门的载体。
    """
    try:
        import uuid
        from datetime import datetime, UTC

        from app.services import world_revision_service as wrsvc

        payload = {
            "type": "civic_standing_changed",
            "schema_version": wrsvc.SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "seq": await wrsvc.current_source_cursor(db),
            "world_revision_id": await wrsvc.current_revision_id(db),
            "resident_slug": slug,
            "old_standing": old_standing,
            "new_standing": new_standing,
            "reason_code": reason_code,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        from app.lab.apply import broadcast_world_changed

        await broadcast_world_changed(payload)
    except Exception:
        logger.warning("civic_standing_changed broadcast failed", exc_info=True)


async def grant_citizenship_batch(
    db, resident_ids, *, reason: str, reason_code: str, actor: str,
    evidence_by_id: dict | None = None,
) -> int:
    """把一批 denizen 升为 citizen。返回实际晋升数。

    形态是「guard 全做完 → 一次 guarded UPDATE → N 行历史 → 一次 commit」::

        UPDATE residents SET resident_type = :new
        WHERE id IN (:ids) AND resident_type = :expected

    ``rowcount != len(ids)`` → **整批回滚 + 告警**（有人在窗口内改过
    ``resident_type``，唯一的并发对手是 admin 手改）。绝不截断执行。

    ⚠️ **调用方契约**：档位翻转走 ``update(...).execution_options(
    synchronize_session=False)``，且本仓的会话是 ``expire_on_commit=False``
    （``tests/conftest.py:119-122``、``app/database.py`` 的 ``async_session``），
    所以**调用方手里的 ORM 对象在本函数返回后仍是旧值**，实体查询也会把同一个
    陈旧对象取回来。需要读新值就 ``await db.refresh(resident)``（
    ``app/routers/admin/residents.py`` 的 ``_edit_resident`` 就是这么做的），
    或者改用列级 ``select(Resident.resident_type)`` / SQL 侧
    ``where(Resident.is_civic_voter)``。
    """
    from sqlalchemy import select, update

    from app.models.resident import Resident
    from app.models.user import User

    ids = sorted(set(resident_ids))
    if not ids:
        return 0

    # 数值闸门 4：取值白名单（写错一个字符的唯一兜底）
    assert_known_types(CIVIC_MEMBER_TYPE, UGC_RESIDENT_TYPE)

    # Guard first: no UPDATE has run yet —— 查库，不信传入对象
    rows = (await db.execute(
        select(Resident.id, Resident.slug, Resident.resident_type)
        .where(Resident.id.in_(ids))
    )).all()
    found = {rid: (slug, rtype) for rid, slug, rtype in rows}
    missing = [rid for rid in ids if rid not in found]
    if missing:
        raise CivicStandingRefused(
            f"grant refused: {len(missing)} unknown resident id(s): {missing}")
    wrong_tier = sorted(rid for rid in ids
                        if found[rid][1] != UGC_RESIDENT_TYPE)
    if wrong_tier:
        raise CivicStandingRefused(
            f"grant refused: {len(wrong_tier)} resident(s) are not in the "
            f"{DENIZEN!r} tier (expected resident_type={UGC_RESIDENT_TYPE!r}): "
            + ", ".join(f"{found[r][0]}={found[r][1]!r}" for r in wrong_tier)
        )
    # 射程防呆：玩家化身即使被 admin 手滑改成 denizen 档也不得被晋升。这是
    # _assert_revocable 第 ① 条的同一段 SQL —— 两个写入口的射程纪律必须对称，
    # 否则「type 已不可信」只在撤销侧成立，晋升侧仍然裸奔（tier 检查会放行，
    # is_ugc_resident 的兜底分支还会把它判成 UGC）。
    avatar_ids = set((await db.execute(
        select(User.player_resident_id).where(User.player_resident_id.in_(ids))
    )).scalars().all())
    if avatar_ids:
        raise CivicStandingRefused(
            f"grant refused: {len(avatar_ids)} target(s) are player avatars "
            f"(users.player_resident_id hits: "
            f"{sorted(found[a][0] for a in avatar_ids if a in found)}). "
            "政治层永不碰玩家化身——2026-07-25 16:53 的事故对象正是这一类；"
            "resident_type 在 admin 手滑那一刻就已不可信。"
        )

    # Guarded UPDATE runs inside its own SAVEPOINT (not a whole-session
    # ``db.rollback()``): a plain ``Session.rollback()`` expires *every*
    # identity-mapped object in the session (``SessionTransaction
    # .rollback()`` calls ``_restore_snapshot(dirty_only=False)`` for a
    # top-level transaction) — including objects the caller holds that were
    # never touched by this write (e.g. the untouched member of the batch).
    # Since ``synchronize_session=False`` never dirties ORM state in the
    # first place, a nested savepoint's rollback (``dirty_only=True``) has
    # nothing to expire and leaves the caller's objects intact. Pattern
    # matches ``app/lab/control_plane.py:458``.
    async with db.begin_nested():
        res = await db.execute(
            update(Resident)
            .where(Resident.id.in_(ids), Resident.resident_type == UGC_RESIDENT_TYPE)
            .values(resident_type=CIVIC_MEMBER_TYPE)
            .execution_options(synchronize_session=False)
        )
        touched = res.rowcount or 0
        if touched != len(ids):
            raise CivicStandingRefused(
                f"grant refused: guarded UPDATE touched {touched} of {len(ids)} "
                "rows — resident_type changed inside the window; whole batch "
                "rolled back (see relation_service.py:214-223 for the pattern)"
            )

    for rid in ids:
        await _write_history(
            db, resident_id=rid, old_standing=DENIZEN, new_standing=CITIZEN,
            reason=reason, reason_code=reason_code, actor=actor,
            evidence=(evidence_by_id or {}).get(rid),
        )
    await db.commit()

    for rid in ids:
        await _emit_standing_changed(
            db, slug=found[rid][0], old_standing=DENIZEN,
            new_standing=CITIZEN, reason_code=reason_code,
        )
    logger.info("civic grant: %d resident(s) promoted by %s (%s)",
                len(ids), actor, reason_code)
    return len(ids)


async def grant_citizenship(
    db, resident, *, reason: str, actor: str, evidence: dict | None = None,
    reason_code: str = "granted",
) -> bool:
    """单条晋升（admin 路由用）。:func:`grant_citizenship_batch` 的薄包装——
    两条路径共用同一份 guard 与同一份写形态，不存在实现漂移。"""
    resident_id = getattr(resident, "id", None)
    if not resident_id:
        raise CivicStandingRefused("grant refused: resident has no id")
    return await grant_citizenship_batch(
        db, [resident_id], reason=reason, reason_code=reason_code, actor=actor,
        evidence_by_id={resident_id: evidence or {}},
    ) == 1


# ═══════════════════════════════════════════════════════════════════════
# 写入口 ②：撤销 —— 防呆（Guard first）
# ═══════════════════════════════════════════════════════════════════════


async def _assert_revocable(db, resident_id: str) -> tuple[str, str]:
    """撤销的射程白名单检查。返回 ``(slug, current_resident_type)``。

    **在第一条 UPDATE 之前**全部做完（照抄 ``seed/reset_builtin_residents.py
    :125-127`` 的 "Guard first: no DELETE has run yet" 姿势），使拒绝是真正的
    no-op。两条设计选择照抄 07-25：**raise 而非静默跳过**、**读数据库而非信
    传入对象**。

    绝对不可被碰的四类 + 一道数值闸门，任一命中即
    :class:`CivicStandingRefused`。
    """
    from sqlalchemy import func, select

    from app.models.civic_standing_history import CivicStandingHistory
    from app.models.resident import Resident
    from app.models.user import User

    row = (await db.execute(
        select(Resident.id, Resident.slug, Resident.resident_type,
               Resident.creator_id)
        .where(Resident.id == resident_id)
    )).first()
    if row is None:
        raise CivicStandingRefused(
            f"revoke refused: no resident with id {resident_id!r}")
    rid, slug, rtype, creator_id = row

    # ① 玩家化身 —— 07-25 事故对象。type 与 FK 是 OR：admin 手滑可以把化身
    #    改成 npc，那一刻 resident_type 已不可信，users.player_resident_id
    #    （app/models/user.py:30）才是权威。
    avatar_hits = (await db.execute(
        select(func.count()).select_from(User)
        .where(User.player_resident_id == rid)
    )).scalar() or 0
    if rtype == PLAYER_RESIDENT_TYPE or avatar_hits:
        raise CivicStandingRefused(
            f"revoke refused: {slug!r} is a player avatar "
            f"(resident_type={rtype!r}, users.player_resident_id hits="
            f"{avatar_hits}). 2026-07-25 16:53 的事故对象正是这一类；政治层"
            "永不碰玩家化身。"
        )
    # ② 内置阵容 —— 被降 = 选举与法定人数熄火
    if creator_id == SYSTEM_CREATOR_ID:
        raise CivicStandingRefused(
            f"revoke refused: {slug!r} is part of the built-in cast "
            f"(creator_id == SYSTEM_CREATOR_ID). 降内置成员会让选举与法定人数"
            "熄火：polis_office_mayor_term_days=0 下的真实稳态是「现任镇长被"
            "永久冻结、再也选不出新人」。"
        )
    # ③ admin preset —— 两个集合之外，本来就不该被政治层动
    if rtype == ADMIN_PRESET_TYPE:
        raise CivicStandingRefused(
            f"revoke refused: {slug!r} is an admin-created {ADMIN_PRESET_TYPE!r} "
            "resident — outside both membership sets by design (U6 待决项)。"
        )
    # ④ 当前不在 citizen 档
    if rtype != CIVIC_MEMBER_TYPE:
        raise CivicStandingRefused(
            f"revoke refused: {slug!r} is not in the {CITIZEN!r} tier "
            f"(resident_type={rtype!r}, expected {CIVIC_MEMBER_TYPE!r})"
        )
    # ⑤ 无晋升记录者 —— 撤销是晋升的严格逆操作，白名单而非泛谓词
    promotions = (await db.execute(
        select(func.count()).select_from(CivicStandingHistory).where(
            CivicStandingHistory.resident_id == rid,
            CivicStandingHistory.new_standing == CITIZEN,
        )
    )).scalar() or 0
    if not promotions:
        raise CivicStandingRefused(
            f"revoke refused: {slug!r} has no promotion record in "
            "civic_standing_history. 撤销是晋升的严格逆操作——白名单，不是泛"
            "谓词。（admin 手工改回 npc 的人会在探针上显示为「无晋升记录的 "
            "UGC-origin 公民」，那是一条有用的红旗。）"
        )
    # 数值闸门 3：选民下限不变式
    electorate = (await db.execute(
        select(func.count()).select_from(Resident).where(Resident.is_civic_voter)
    )).scalar() or 0
    floor = max(min_peers() + 1, min_electorate())
    if electorate - 1 < floor:
        raise CivicStandingRefused(
            f"revoke refused: electorate would drop to {electorate - 1}, below "
            f"the floor max(min_peers+1, CIVIC_MIN_ELECTORATE) = {floor}. "
            "open_election 需要 ≥2 候选（election_service.py:62-63）；这条不"
            "变式在未来做逐出时同样成立。"
        )
    return slug, rtype


# ═══════════════════════════════════════════════════════════════════════
# 写入口 ②：撤销 —— 有序复合事务
# ═══════════════════════════════════════════════════════════════════════


async def _assert_demotion_invariants(db, *, resident_id: str, slug: str) -> None:
    """步骤 6 的自查：三处镇长表示都不指向他；人口口径不变、政治口径已收回。

    全部用**列级 SELECT**（不是 ORM 实体），绕开 identity map——步骤 4 的
    ``update()`` 带 ``synchronize_session=False``，会话里的实体对象仍是旧值。
    """
    import json

    from sqlalchemy import func, select

    from app.models.office import Office
    from app.models.resident import Resident
    from app.models.system_config import SystemConfig

    rtype = (await db.execute(
        select(Resident.resident_type).where(Resident.id == resident_id)
    )).scalar_one()
    if rtype != UGC_RESIDENT_TYPE:
        raise CivicStandingRefused(
            f"demotion invariant broken: {slug!r} landed on resident_type "
            f"{rtype!r}, expected {UGC_RESIDENT_TYPE!r}")
    if rtype not in SIM_RESIDENT_TYPES:
        raise CivicStandingRefused(
            f"demotion invariant broken: {slug!r} fell out of the world "
            "population (is_autonomous would be False) — 撤销不是移出世界")
    if rtype in CIVIC_VOTER_TYPES:
        raise CivicStandingRefused(
            f"demotion invariant broken: {slug!r} still holds political rights")

    held = (await db.execute(
        select(func.count()).select_from(Office).where(
            Office.fill_strategy == POLITICAL_FILL_STRATEGY,
            Office.holder_slug == slug,
        )
    )).scalar() or 0
    if held:
        raise CivicStandingRefused(
            f"demotion invariant broken: {slug!r} still holds {held} elected "
            "office row(s)")

    meta = (await db.execute(
        select(Resident.meta_json).where(Resident.id == resident_id)
    )).scalar_one() or {}
    if meta.get("mayor"):
        raise CivicStandingRefused(
            f"demotion invariant broken: {slug!r} still carries "
            "meta_json['mayor'] — 工资倍率的唯一读点（duty_service.py:172-173）")

    cfg_value = (await db.execute(
        select(SystemConfig.value).where(SystemConfig.key == "current_mayor")
    )).scalar_one_or_none()
    if cfg_value is not None:
        try:
            current = json.loads(cfg_value)
        except (TypeError, ValueError):
            current = None
        if current == slug:
            raise CivicStandingRefused(
                f"demotion invariant broken: system_config['current_mayor'] "
                f"still points at {slug!r}")


async def revoke_citizenship(
    db, resident, *, reason: str, actor: str, tier: str = "demote",
    reason_code: str = "revoked",
) -> bool:
    """撤销公民权。``tier="demote"``（本轮）| ``"exile"``（占位）。

    **有序复合事务，顺序不可颠倒**::

        0. 防呆（第一条 UPDATE 之前全部做完）
        1. 卸民选职务（fill_strategy='election' 且 holder_slug=:slug）
        2. 清 meta_json['mayor']（按 slug 直查，不用集合谓词做 WHERE）
        3. 清 system_config['current_mayor']（仅当指向此人）
        4. 改档位（guarded UPDATE）
        5. 写 civic_standing_history 一行
        6. 断言
        7. commit + 广播

    若先改档位再清理，``meta_json['mayor']`` 在逐出档会永久卡死（清扫扫不到
    他），期间 ``install_mayor()`` 清他人标志时也会跳过他，可产生「两个
    ``meta_json['mayor']=True``」并双份工资倍率。

    三条「不得」：不得调用 ``OfficeService.vacate()``（自带 commit，且 gate 关
    时命中 0 行会跳过 legacy 清理）；不得复用 ``_clear_mayor_legacy_stores``
    （用 ``is_autonomous`` 这个集合谓词去清「刚离开集合的人」）；不得用
    ``ConfigService.set()``（自带 commit，会把复合事务劈成两半）。

    **劳动职务不受影响**：``town_clerk`` / ``postman`` / ``doctor`` 的 offices
    行与 ``meta_json['duty']`` 一律不动。**永不 DELETE。**

    ⚠️ **调用方契约（成功路径）**（与 :func:`grant_citizenship_batch` 同）：
    步骤 4 的档位翻转走 ``update(...).execution_options(synchronize_session=
    False)``，而本仓的会话是 ``expire_on_commit=False``，所以**调用方传进来的
    ORM 对象在本函数返回后仍是旧值**，``select(Resident)`` 这类实体查询也会把
    同一个陈旧对象取回来。要读新值就 ``await db.refresh(resident)``（
    ``_edit_resident`` 就是这么做的），或改用列级 SELECT / SQL 侧谓词。步骤 6
    的 :func:`_assert_demotion_invariants` 全部用列级 SELECT 正是这个原因。

    ⚠️ **调用方契约（异常路径）**：任何一步失败（防呆之后的 guard UPDATE
    rowcount 不符、步骤 6 的自查不过）都会命中下面的 ``except Exception:
    await db.rollback(); raise``——这是真正的错误路径（本函数认为复合事务已经
    坏了，必须整体回滚，不是某一步「按预期拒绝」还要保留会话继续用），
    ``db.rollback()`` 在这属于正确用法。但它对**顶层事务** 调用
    ``_restore_snapshot(dirty_only=False)``，会 expire 整个 identity map——不
    只是本函数碰过的对象，调用方在同一 session 里更早加载的任何 ORM 实体都会
    被 expire。之后对它们做一次同步属性读会触发隐式 lazy-reload，在没有
    greenlet 上下文的地方炸出 ``sqlalchemy.exc.MissingGreenlet``（与
    F3 线 ``office_audit.py`` 记录的是同一类故障，只是触发点是这里的守卫
    UPDATE 分支）。**调用方在捕获到本函数抛出的异常之后，不得直接读之前在同一
    session 里加载过的任何 ORM 对象的属性**——要么 ``await db.refresh(obj)``
    先刷新，要么重新 SELECT。
    """
    if tier == EXILED or tier == "exile":
        raise NotImplementedError(
            "revoke_citizenship(tier='exile') 是预留签名：分档清理表已按两档"
            "写好（住房 home_location_id / tile 占用 / 劳动职务全撤 + is_in_town "
            "收窄），v1 只实现 demote 档。逐出上线时是填空，不是改签名。"
        )
    if tier != "demote":
        raise ValueError(
            f"unknown revoke tier {tier!r}; expected 'demote' or 'exile'")

    import json
    from datetime import datetime, UTC

    from sqlalchemy import select, update
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.office import Office
    from app.models.resident import Resident
    from app.models.system_config import SystemConfig

    resident_id = getattr(resident, "id", None)
    if not resident_id:
        raise CivicStandingRefused("revoke refused: resident has no id")

    # 0. Guard first: no UPDATE has run yet
    slug, current_type = await _assert_revocable(db, resident_id)
    assert_known_types(current_type, UGC_RESIDENT_TYPE)   # 数值闸门 4

    try:
        # 1. 卸民选职务。只 election 档；带 holder 校验（gate 关时 offices
        #    可能留着迁移 046 的陈旧值，无条件 vacate 会罢免错的人）。
        #    正确性不依赖 polis_office_enabled 的取值。
        await db.execute(
            update(Office)
            .where(Office.fill_strategy == POLITICAL_FILL_STRATEGY,
                   Office.holder_slug == slug)
            .values(holder_slug=None, term_ends_at=None,
                    updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        # 2. 清 meta_json['mayor'] —— 按 slug 直查（通用约束：清理「已离开
        #    集合 S 的居民」不得用 S 本身做 WHERE）
        target = (await db.execute(
            select(Resident).where(Resident.slug == slug)
        )).scalar_one()
        meta = dict(target.meta_json or {})
        if meta.pop("mayor", None) is not None:
            target.meta_json = meta
            flag_modified(target, "meta_json")
        # 3. 清 system_config['current_mayor'] —— 仅当当前值指向此人
        cfg = (await db.execute(
            select(SystemConfig).where(SystemConfig.key == "current_mayor")
        )).scalar_one_or_none()
        if cfg is not None:
            try:
                current = json.loads(cfg.value)
            except (TypeError, ValueError):
                current = None
            if current == slug:
                cfg.value = json.dumps(None)
                cfg.updated_by = actor
                cfg.updated_at = datetime.now(UTC)
        # 4. 改档位（guarded UPDATE）
        res = await db.execute(
            update(Resident)
            .where(Resident.id == resident_id,
                   Resident.resident_type == current_type)
            .values(resident_type=UGC_RESIDENT_TYPE)
            .execution_options(synchronize_session=False)
        )
        if (res.rowcount or 0) != 1:
            raise CivicStandingRefused(
                f"revoke refused: guarded UPDATE touched {res.rowcount} rows "
                f"for {slug!r} — resident_type changed inside the window")
        # 5. 历史行
        await _write_history(
            db, resident_id=resident_id, old_standing=CITIZEN,
            new_standing=DENIZEN, reason=reason, reason_code=reason_code,
            actor=actor, evidence=None,
        )
        # 6. 断言（flush 让前面的 ORM 改动落到本事务里再自查）
        await db.flush()
        await _assert_demotion_invariants(db, resident_id=resident_id, slug=slug)
    except Exception:
        await db.rollback()
        raise
    # 7. commit + 广播
    await db.commit()
    await _emit_standing_changed(
        db, slug=slug, old_standing=CITIZEN, new_standing=DENIZEN,
        reason_code=reason_code,
    )
    logger.info("civic revoke: %s demoted by %s (%s)", slug, actor, reason_code)
    return True


__all__ = [
    # 既有的两个集合边界
    "CIVIC_VOTER_TYPES", "SIM_RESIDENT_TYPES", "UGC_RESIDENT_TYPE",
    # 档位（standing）
    "CITIZEN", "DENIZEN", "EXILED", "CIVIC_STANDINGS", "CIVIC_MEMBER_TYPE",
    "STANDING_TO_TYPE", "TYPE_TO_STANDING",
    "civic_standing", "standing_to_type", "assert_known_types",
    # 出身（provenance）
    "PLAYER_RESIDENT_TYPE", "ADMIN_PRESET_TYPE", "ADMIN_PRESET_CREATOR_ID",
    "SYSTEM_CREATOR_ID", "UGC_ORIGINS", "NON_UGC_ORIGINS", "is_ugc_resident",
    "ugc_filter", "POLITICAL_FILL_STRATEGY",
    # 防呆
    "CivicStandingRefused",
    # 写入口
    "grant_citizenship", "grant_citizenship_batch", "revoke_citizenship",
    # 运行时旋钮
    "promotion_mode", "min_world_days", "min_peers", "min_familiarity",
    "peer_seasoning_world_days", "promotion_max_per_run",
    "promotion_breaker_fraction", "promotion_breaker_min_abs",
    "min_electorate", "min_tenure_world_days",
    "promotion_cooldown_world_days", "auto_demotion_enabled",
]
