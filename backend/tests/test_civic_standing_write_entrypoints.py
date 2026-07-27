"""F2 Task 10 —— resident_type 收敛为唯一写入口。

列上没有 CHECK（app/models/resident.py:55 是裸 String(20)），代码就是最后
一道闸。admin/residents.py:117-118 曾是仓库里唯一的运行时裸赋值，也是 F2
批量 UPDATE 唯一的并发对手（正面样板：relation_service.py:214-223；反面
样板：admin/residents.py:103-127 的读-改-写，已在本任务修复）。

结构性守卫仿 tests/test_ugc_resident_no_political_rights.py:69-88 的 AST 扫描，
把覆盖面从「Resident(...) 构造」扩展到「*.resident_type = ...」赋值。

2026-07-27 协调者裁定加宽：上面这条只封了「属性赋值」一种绕过形态。批量
UPDATE（`update(Resident).values(resident_type=...)` /
`.values({"resident_type": ...})`）与直接构造
（`Resident(resident_type="npc", ...)`）是另外两条同样能绕开
grant_citizenship/revoke_citizenship 防呆的路——这正是本线已经撞见过九次的
「看着像守卫但没守住」的第十次样本，理由见下面的 guard-of-the-guard 小节。
"""
import ast
import pathlib

import pytest
from sqlalchemy import func, select

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.user import User
from app.services import civic_membership as cm
from app.services.auth_service import create_token

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 允许给 resident_type 赋值的文件（相对 backend/）。加进来必须写理由。
_ASSIGNMENT_ALLOWLIST = {
    "app/services/civic_membership.py": "两个写入口所在的模块",
}


# ── 写入口守卫探测器（形态①：属性赋值）───────────────────────────────────
#
# 抽出成独立函数，让「全仓真实扫描」与「喂源码文本的 hermetic 单元测试」共用
# 同一份判定逻辑，两边不会各写一份而漂移（同 test_reputation_population_scope
# .py 的 _npc_literal_offenders 抽取姿势）。


def _assignment_offenders(tree: ast.AST, label: str) -> list[str]:
    """形态①：`<expr>.resident_type = ...`。target 前缀（裸名 / 链式属性 /
    下标 / 调用结果）与判定无关，只看最外层是不是 `resident_type` 属性；
    `Assign.targets` 本就是一张表，`a = b.resident_type = ...` 这种复合赋值
    天然被覆盖，不需要特殊处理。"""
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr == "resident_type":
                offenders.append(f"{label}:{node.lineno}")
    return offenders


def _offenders_in_source(source: str, label: str = "<source>") -> list[str]:
    """给 hermetic 单元测试用：喂源码文本，不碰真实文件。

    TODO(2026-07-27 协调者裁定加宽)：此刻只接了形态①（属性赋值）。批量
    UPDATE 的 `.values(resident_type=...)` / `.values({"resident_type":
    ...})` 与直接构造 `Resident(resident_type="npc", ...)` 还没接进来——这
    正是下面 `test_guard_catches_every_evasive_write_shape` 要钉住的两个
    缺口，本轮红提交先让它们可见地失败。
    """
    return _assignment_offenders(ast.parse(source, filename=label), label)


def test_only_civic_membership_assigns_resident_type():
    offenders = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        rel = str(path.relative_to(BACKEND_ROOT))
        if rel in _ASSIGNMENT_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(_assignment_offenders(tree, rel))
    assert offenders == [], (
        "resident_type 只许由 civic_membership 的两个写入口改写（列上没有 "
        f"CHECK，代码是最后一道闸）：{offenders}")


# ── Guard-of-the-guard（2026-07-27 协调者裁定加宽）───────────────────────
#
# 喂源码文本给探测器，不碰真实文件——hermetic，跑得快，且直接钉住"这个探测器
# 认不认识这种句法形状"，与"全仓当下有没有这种代码"（上面那条真实扫描）是两
# 件事。

_EVASIVE_WRITE_SHAPES = {
    # 形态①——既有覆盖面，协调者点名的两种 target 形状作为回归锁一并钉住：
    # 此刻已经能通过（_assignment_offenders 不看 target 前缀），保留在这组
    # 参数化里防止未来重构悄悄收窄。
    "chained-attr-assign": 'obj.attr.resident_type = "npc"\n',
    "subscripted-chained-attr-assign": 'residents[0].resident_type = "npc"\n',
    "multi-target-assign": 'a = b.resident_type = "npc"\n',
    "augassign": 'resident.resident_type += "x"\n',
    "annassign": 'resident.resident_type: str = "npc"\n',
    # 形态②——批量 UPDATE，此刻探测器还没接，应该红。
    "bulk-update-keyword": (
        'update(Resident).where(Resident.id == rid)'
        '.values(resident_type="npc")\n'),
    "bulk-update-dict": (
        'update(Resident).where(Resident.id == rid)'
        '.values({"resident_type": "npc"})\n'),
    # 形态③——直接构造，此刻探测器还没接，应该红。
    "construction-npc-literal": (
        'Resident(resident_type="npc", creator_id=cid)\n'),
}


@pytest.mark.parametrize("source", _EVASIVE_WRITE_SHAPES.values(),
                        ids=_EVASIVE_WRITE_SHAPES.keys())
def test_guard_catches_every_evasive_write_shape(source):
    """唯一写入口条款只封了「属性赋值」一种绕过形态。批量 UPDATE 与直接构造
    是另外两条同样能绕开 grant_citizenship/revoke_citizenship 防呆的路——这
    正是本线已经撞见过九次的『看着像守卫但没守住』的第十次样本。"""
    offenders = _offenders_in_source(source)
    assert offenders, f"guard failed to flag an evasive write shape: {source!r}"


_EXEMPT_WRITE_SHAPES = {
    # 两个写入口自己的批量 UPDATE 调用——协调者点名的最高风险点：加宽形态②
    # 之后这两条绝不能被误伤，否则 civic_membership.py 自己会被自己的守卫
    # 挡住。此刻（形态②还没接）自然不被误报，先钉基线。
    "civic-membership-grant-update": (
        'update(Resident).where(Resident.id == rid)'
        '.values(resident_type=CIVIC_MEMBER_TYPE)\n'),
    "civic-membership-revoke-update": (
        'update(Resident).where(Resident.id == rid)'
        '.values(resident_type=UGC_RESIDENT_TYPE)\n'),
    # 五个合法 UGC 构造站点的形状：符号引用，不是字面量。
    "ugc-construction-symbolic": (
        'Resident(resident_type=UGC_RESIDENT_TYPE, creator_id=cid)\n'),
    # admin _create_preset 的形参透传：符号引用（Name），不是字面量。
    "admin-preset-passthrough": (
        'Resident(resident_type=resident_type, creator_id=cid)\n'),
    # onboarding_service.py 的玩家化身创建：合法的字面量构造，但值不是
    # "npc"——CIVIC_VOTER_TYPES 里唯一的政治权利取值，形态③只收窄到这一个。
    "onboarding-player-literal": (
        'Resident(resident_type="player", creator_id=cid)\n'),
    "membership-in-operator": (
        'x = resident_type in ("preset", "npc", UGC_RESIDENT_TYPE)\n'),
    "values-call-unrelated-keyword": (
        'stmt.values(status="idle")\n'),
    "values-call-unrelated-dict-key": (
        'stmt.values({"status": "idle"})\n'),
}


@pytest.mark.parametrize("source", _EXEMPT_WRITE_SHAPES.values(),
                        ids=_EXEMPT_WRITE_SHAPES.keys())
def test_guard_does_not_flag_mechanism_exempt_write_shapes(source):
    """加宽前先钉住基线：这八类『按机制豁免』的写法此刻已经不被误报，防止
    下一步接入形态②③时误伤——尤其是两个写入口自己的批量 UPDATE 调用。"""
    assert _offenders_in_source(source) == []


def test_every_resident_construction_still_sets_the_type_explicitly():
    """既有守卫的复述：创建路径必须显式给 resident_type（依赖模型默认值正是
    2026-07-25 把选票发给 UGC 居民的根因）。"""
    offenders = []
    for sub in ("app", "seed"):
        for path in (BACKEND_ROOT / sub).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Name) and fn.id == "Resident"):
                    continue
                if not any(kw.arg == "resident_type" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")
    assert offenders == []


# ── admin 路由的功能验证 ───────────────────────────────────────────────

async def _admin(db):
    u = User(name="管理员", email="admin@t.com", is_admin=True)
    db.add(u)
    await db.commit()
    return u


def _res(slug, rtype, *, creator_id="u1", meta=None):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta)


@pytest.mark.anyio
async def test_admin_promotion_goes_through_the_write_entrypoint(client, db_session):
    admin = await _admin(db_session)
    r = _res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()

    resp = await client.put(
        f"/admin/residents/{r.id}",
        json={"resident_type": cm.CIVIC_MEMBER_TYPE},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 200, resp.text

    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.CIVIC_MEMBER_TYPE
    row = (await db_session.execute(select(CivicStandingHistory))).scalar_one()
    assert row.new_standing == cm.CITIZEN
    assert row.actor.startswith("admin:"), "actor 必须带 admin 的 user id"


@pytest.mark.anyio
async def test_admin_demotion_goes_through_the_write_entrypoint(client, db_session):
    admin = await _admin(db_session)
    db_session.add_all([_res(f"b{i}", cm.CIVIC_MEMBER_TYPE,
                             creator_id=cm.SYSTEM_CREATOR_ID) for i in range(6)])
    r = _res("ugc-1", cm.CIVIC_MEMBER_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()
    await cm._write_history(
        db_session, resident_id=r.id, old_standing=cm.DENIZEN,
        new_standing=cm.CITIZEN, reason=None, reason_code="threshold_met",
        actor="civic_promotion", evidence=None)
    await db_session.commit()

    resp = await client.put(
        f"/admin/residents/{r.id}",
        json={"resident_type": cm.UGC_RESIDENT_TYPE},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 200, resp.text
    rtype = (await db_session.execute(
        select(Resident.resident_type).where(Resident.id == r.id))).scalar_one()
    assert rtype == cm.UGC_RESIDENT_TYPE


@pytest.mark.anyio
async def test_admin_cannot_demote_a_builtin(client, db_session):
    """射程纪律：防呆对 admin 同样生效，返回 409 而不是静默成功。"""
    admin = await _admin(db_session)
    db_session.add_all([_res(f"b{i}", cm.CIVIC_MEMBER_TYPE,
                             creator_id=cm.SYSTEM_CREATOR_ID) for i in range(6)])
    await db_session.commit()
    b = (await db_session.execute(
        select(Resident).where(Resident.slug == "b0"))).scalar_one()

    resp = await client.put(
        f"/admin/residents/{b.id}",
        json={"resident_type": cm.UGC_RESIDENT_TYPE, "district": "free"},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 409, resp.text
    await db_session.refresh(b)
    assert b.resident_type == cm.CIVIC_MEMBER_TYPE
    assert b.district == "town_hall", "拒绝必须是整请求的 no-op"


@pytest.mark.anyio
async def test_admin_cannot_set_an_arbitrary_type(client, db_session):
    admin = await _admin(db_session)
    r = _res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()

    resp = await client.put(
        f"/admin/residents/{r.id}",
        json={"resident_type": "player"},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 409, resp.text
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_admin_edit_of_other_fields_still_works(client, db_session):
    admin = await _admin(db_session)
    r = _res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()

    resp = await client.put(
        f"/admin/residents/{r.id}",
        json={"district": "free", "status": "sleeping"},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(r)
    assert (r.district, r.status) == ("free", "sleeping")
    assert r.resident_type == cm.UGC_RESIDENT_TYPE


@pytest.mark.anyio
async def test_admin_setting_the_same_type_is_a_noop(client, db_session):
    admin = await _admin(db_session)
    r = _res("ugc-1", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
    db_session.add(r)
    await db_session.commit()

    resp = await client.put(
        f"/admin/residents/{r.id}",
        json={"resident_type": cm.UGC_RESIDENT_TYPE},
        headers={"Authorization": f"Bearer {create_token(admin.id)}"},
    )
    assert resp.status_code == 200, resp.text
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
