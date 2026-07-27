"""F2 Task 2 —— 出身 × 档位二维模型的常量层与派生函数。

档位有序三档（citizen > denizen > exiled），v1 仍编码在 resident_type 单列：
不加列、不加取值。不新增第 5 个 type 的理由是地图与感知**不读 type**——公开
名录是全表（app/services/resident_service.py:6-18）、tile 占用也是全表
（app/services/resident_placement.py:104-111/:157-160），新增取值只会掉出
SIM_RESIDENT_TYPES，产出「仍在地图上、仍被搭话，只是自己不再 tick」的活体
雕像。逐出要收窄的是第四族谓词 is_in_town，不是这两个集合。
"""
import ast
import pathlib

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.services import civic_membership as cm

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _res(slug, rtype, *, creator_id="u1", meta=None):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id,
                    tile_x=1, tile_y=1, meta_json=meta)


# ── 档位枚举与编码 ─────────────────────────────────────────────────────

def test_three_ordered_standings():
    assert cm.CIVIC_STANDINGS == ("citizen", "denizen", "exiled")
    assert (cm.CITIZEN, cm.DENIZEN, cm.EXILED) == cm.CIVIC_STANDINGS


def test_citizen_tier_encodes_as_the_voter_type():
    """CIVIC_MEMBER_TYPE 是唯一允许出现的 'npc' 字面量来源：resident_type 是
    裸 String(20)、无 enum 无 CHECK（models/resident.py:55），写错一个字符
    ('npc ') 就同时掉出两个集合。"""
    assert cm.CIVIC_MEMBER_TYPE == "npc"
    assert cm.CIVIC_MEMBER_TYPE in cm.CIVIC_VOTER_TYPES
    assert cm.CIVIC_MEMBER_TYPE in cm.SIM_RESIDENT_TYPES
    assert cm.STANDING_TO_TYPE == {cm.CITIZEN: cm.CIVIC_MEMBER_TYPE,
                                   cm.DENIZEN: cm.UGC_RESIDENT_TYPE}
    assert cm.TYPE_TO_STANDING == {cm.CIVIC_MEMBER_TYPE: cm.CITIZEN,
                                   cm.UGC_RESIDENT_TYPE: cm.DENIZEN}


def test_no_fifth_resident_type_value_was_introduced():
    """EXILED 档刻意不映射到任何 resident_type 取值。"""
    assert cm.EXILED not in cm.STANDING_TO_TYPE
    assert "exiled" not in cm.SIM_RESIDENT_TYPES
    assert "exiled" not in cm.CIVIC_VOTER_TYPES


def test_civic_standing_reads_the_tier_off_a_resident():
    assert cm.civic_standing(_res("b1", "npc", creator_id=cm.SYSTEM_CREATOR_ID)) == cm.CITIZEN
    assert cm.civic_standing(_res("u1", cm.UGC_RESIDENT_TYPE)) == cm.DENIZEN


@pytest.mark.parametrize("rtype", ["player", "preset", "npc ", "", None])
def test_civic_standing_refuses_types_outside_the_tier_model(rtype):
    """player 由第三族谓词（!= "player"）管辖、preset 是待决项、其余是写错的
    字面量——都不该被当成某个档位悄悄处理。"""
    with pytest.raises(ValueError):
        cm.civic_standing(_res("x", rtype))


def test_standing_to_type_reserves_the_exile_tier():
    assert cm.standing_to_type(cm.CITIZEN) == cm.CIVIC_MEMBER_TYPE
    assert cm.standing_to_type(cm.DENIZEN) == cm.UGC_RESIDENT_TYPE
    with pytest.raises(NotImplementedError):
        cm.standing_to_type(cm.EXILED)
    with pytest.raises(ValueError):
        cm.standing_to_type("citizen-ish")


def test_assert_known_types_is_the_value_whitelist_gate():
    cm.assert_known_types(cm.CIVIC_MEMBER_TYPE, cm.UGC_RESIDENT_TYPE)
    with pytest.raises(cm.CivicStandingRefused):
        cm.assert_known_types("npc ")           # 尾空格：闸门 4 要拦的正是它
    with pytest.raises(cm.CivicStandingRefused):
        cm.assert_known_types("player")


# ── UGC 判定（T2 与 F2 共用同一份实现） ────────────────────────────────

def test_system_creator_id_matches_the_seed_constant():
    """T2 脚本与 F2 任务共用这个常量；它必须与种子模块逐字相等，否则内置阵容
    会被当成 UGC 纳入晋升/撤销射程。"""
    from seed.preset_characters import SYSTEM_USER_ID
    assert cm.SYSTEM_CREATOR_ID == SYSTEM_USER_ID


def test_ugc_origins_are_the_three_creation_paths():
    assert cm.UGC_ORIGINS == frozenset({"forge", "import", "quick_forge"})


def test_non_ugc_origins_include_onboarding():
    """``"onboarding"`` 是玩家化身的出身（onboarding_service.py:91），必须与
    ``"preset"`` 并列判否。

    否则：admin 手滑把化身的 type 改成 ``resident`` 之后，兜底分支
    ``return creator_id is not None`` 会把它判成 UGC（化身的 creator_id 是真实
    user id）→ 进 select_promotions 的候选面 → 被夜间任务自动授予投票权，而且
    此后 _assert_revocable 的玩家化身 FK 复核会拒绝撤销，人就永久卡在 citizen 档。
    """
    assert cm.NON_UGC_ORIGINS == frozenset({"preset", "onboarding"})
    assert not (cm.UGC_ORIGINS & cm.NON_UGC_ORIGINS)


@pytest.mark.parametrize("meta,creator,expected", [
    ({"origin": "forge"}, "u1", True),
    ({"origin": "import"}, "u1", True),
    ({"origin": "quick_forge"}, "u1", True),
    # 极老的 UGC 行不保证带 origin —— 有真实 creator_id 即算
    (None, "u1", True),
    # 账号注销后 creator_id 变 NULL（迁移 045）且无 origin —— 保守判否，
    # 由 T2 的「残差人工点名复核」兜底
    (None, None, False),
    # admin preset：creator_id 是字面量 "system"，origin 也写 "preset"
    ({"origin": "preset"}, "system", False),
    # 被篡改 type 的玩家化身：creator_id 是真实 user id，只有 origin 认得出它
    ({"origin": "onboarding"}, "u1", False),
])
def test_is_ugc_resident_covers_the_three_valued_creator_id(meta, creator, expected):
    assert cm.is_ugc_resident(_res("x", cm.UGC_RESIDENT_TYPE,
                                   creator_id=creator, meta=meta)) is expected


def test_is_ugc_resident_excludes_builtins_and_avatars():
    """内置阵容与 admin preset 同写 meta_json.origin == "preset"
    （seed/preset_characters.py:1237 与 routers/admin/residents.py:148），
    所以 provenance 判定以 creator_id 为主键。"""
    builtin = _res("b1", "npc", creator_id=cm.SYSTEM_CREATOR_ID,
                   meta={"origin": "preset", "is_preset": True})
    assert cm.is_ugc_resident(builtin) is False
    avatar = _res("a1", "player", creator_id="u1")
    assert cm.is_ugc_resident(avatar) is False


@pytest.mark.anyio
async def test_ugc_filter_is_a_sql_superset_of_the_python_predicate(db_session):
    """SQL 只做粗筛（meta_json 是 sa.JSON，跨 sqlite/PG 没有可移植的 JSON 路径
    查询），精确判定必须再过一遍 is_ugc_resident。粗筛必须是超集，否则会漏人。"""
    rows = [
        _res("ugc-forge", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"}),
        _res("ugc-old", cm.UGC_RESIDENT_TYPE, meta=None),
        _res("ugc-orphan", cm.UGC_RESIDENT_TYPE, creator_id=None),
        _res("builtin", "npc", creator_id=cm.SYSTEM_CREATOR_ID,
             meta={"origin": "preset"}),
        _res("adminpreset", "preset", creator_id="system",
             meta={"origin": "preset"}),
        _res("avatar", "player"),
        # admin 手滑把化身改成 resident 档：SQL 粗筛认不出（creator_id 是真实
        # user id），只有 origin == "onboarding" 能挡下它
        _res("tampered-avatar", cm.UGC_RESIDENT_TYPE,
             meta={"origin": "onboarding"}),
    ]
    db_session.add_all(rows)
    await db_session.commit()

    coarse = (await db_session.execute(
        select(Resident).where(cm.ugc_filter())
    )).scalars().all()
    coarse_slugs = {r.slug for r in coarse}
    exact = {r.slug for r in coarse if cm.is_ugc_resident(r)}

    assert "builtin" not in coarse_slugs
    assert "adminpreset" not in coarse_slugs
    assert "avatar" not in coarse_slugs
    assert exact == {"ugc-forge", "ugc-old"}
    # 超集性质：孤儿行与被篡改的化身都进了粗筛、被精确判定挡掉，而不是在 SQL
    # 层就消失（SQL 挡不住它们，所以 is_ugc_resident 必须是最终判据）
    assert "ugc-orphan" in coarse_slugs
    assert "tampered-avatar" in coarse_slugs


# ── env 旋钮 ───────────────────────────────────────────────────────────

def test_promotion_mode_defaults_to_off(monkeypatch):
    monkeypatch.delenv("CIVIC_PROMOTION_MODE", raising=False)
    assert cm.promotion_mode() == "off"
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "  Shadow ")
    assert cm.promotion_mode() == "shadow"


def test_auto_demotion_defaults_off(monkeypatch):
    monkeypatch.delenv("CIVIC_AUTO_DEMOTION_ENABLED", raising=False)
    assert cm.auto_demotion_enabled() is False
    monkeypatch.setenv("CIVIC_AUTO_DEMOTION_ENABLED", "true")
    assert cm.auto_demotion_enabled() is True


def test_numeric_knobs_fall_back_on_garbage(monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "not-a-number")
    assert cm.min_peers() == 3
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.35")
    assert cm.min_familiarity() == 0.35


def test_familiarity_threshold_must_not_collide_with_circle_threshold(monkeypatch):
    """θ 不要取 0.3 —— realism_circle_threshold = 0.3（config.py:512）是圈子
    检测的强边阈值，撞上去会让两套语义纠缠。"""
    monkeypatch.delenv("CIVIC_PROMOTION_MIN_FAMILIARITY", raising=False)
    from app.config import settings
    assert cm.min_familiarity() != settings.realism_circle_threshold


def test_breaker_has_an_absolute_floor(monkeypatch):
    """熔断的**绝对下限**。只按比例算，小镇规模下熔断会恒响：生产内置阵容
    ≈10-11 位公民 × 0.20 ≈ 2.2，一夜 3 个合法候选就整批拒绝，而
    ``CIVIC_PROMOTION_MAX_PER_RUN`` 默认 5 永远够不着——两道闸门互相吞掉，
    闸门 1 变成死代码。下限让「小批量放行、大批量熔断」两个语义都活着。
    """
    monkeypatch.delenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", raising=False)
    assert cm.promotion_breaker_min_abs() == 3
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", "8")
    assert cm.promotion_breaker_min_abs() == 8
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", "-1")
    assert cm.promotion_breaker_min_abs() == 0     # 负值 = 只按比例判


def test_min_electorate_floor_is_at_least_three(monkeypatch):
    """open_election 需要 ≥2 候选（election_service.py:62-63），下限低于 3 时
    撤销可以把小镇的选举机制打死。"""
    monkeypatch.delenv("CIVIC_MIN_ELECTORATE", raising=False)
    assert cm.min_electorate() >= 3
    monkeypatch.setenv("CIVIC_MIN_ELECTORATE", "1")
    assert cm.min_electorate() >= 3


def test_hysteresis_knobs_are_at_least_one_poll_lifetime(monkeypatch):
    """一张 poll 开 civic_poll_days=3 真实天 = 12 世界日（k=4）。最短任期与
    冷却期小于它，单张 poll 生命周期内公民权仍可翻转。"""
    for name in ("CIVIC_MIN_TENURE_WORLD_DAYS",
                 "CIVIC_PROMOTION_COOLDOWN_WORLD_DAYS"):
        monkeypatch.delenv(name, raising=False)
    assert cm.min_tenure_world_days() >= 12
    assert cm.promotion_cooldown_world_days() >= 12


# ── 层次约束（模型层导入本模块，本模块不许反向依赖） ───────────────────

def test_module_top_level_imports_stay_lazy():
    """app/models/resident.py:8 在模型层导入本模块。任何顶层的 app.models.* /
    app.config 导入都会造成循环导入，必须写在函数体内。"""
    src = (BACKEND_ROOT / "app" / "services" / "civic_membership.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    offenders = []
    for node in tree.body:            # 只看模块顶层
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names
                          if a.name.startswith(("app.models", "app.config",
                                                "seed."))]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(("app.models", "app.config", "seed")):
                offenders.append(node.module)
    assert offenders == [], (
        f"civic_membership 顶层导入了 {offenders} —— 会与 models/resident.py:8 "
        "构成循环导入；把它们挪进函数体")
