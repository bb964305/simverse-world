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


# ── 写入口守卫探测器（三种写形态）───────────────────────────────────────
#
# 抽出成独立函数，让「全仓真实扫描」与「喂源码文本的 hermetic 单元测试」共用
# 同一份判定逻辑，两边不会各写一份而漂移（同 test_reputation_population_scope
# .py 的 _npc_literal_offenders 抽取姿势）。
#
# 三个探测器都不用文件路径白名单——civic_membership.py 能通过纯粹是因为它
# 的两个写入口结构上不落在任何一个探测器的判据里，见下面
# test_civic_membership_itself_needs_no_path_exemption 的逐条证明。


def _assignment_offenders(tree: ast.AST, label: str) -> list[str]:
    """形态①：`<expr>.resident_type = ...`。target 前缀（裸名 / 链式属性 /
    下标 / 调用结果）与判定无关，只看最外层是不是 `resident_type` 属性；
    `Assign.targets` 本就是一张表，`a = b.resident_type = ...` 这种复合赋值
    天然被覆盖，不需要特殊处理。civic_membership.py 的两个写入口从不直接
    赋值（改档位一律走 `update().values()`），这条探测器扫它本身就是空的
    ——不需要为它开豁免。"""
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


def _is_str_literal(node: ast.expr) -> bool:
    """任何硬编码字符串，不论内容——批量 UPDATE 探测器（形态②）用它，因为
    **没有任何已知合法调用会传字面量**：civic_membership.py 自己的两次
    `.values(resident_type=...)` 传的都是符号常量（`CIVIC_MEMBER_TYPE` /
    `UGC_RESIDENT_TYPE`），不是 `ast.Constant`。"""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_npc_literal(node: ast.expr) -> bool:
    """唯一政治权利取值的字面量（`CIVIC_MEMBER_TYPE`，`CIVIC_VOTER_TYPES`
    里唯一的元素）。比 :func:`_is_str_literal` 更窄，是有意的：构造
    （形态③）有一个赋值/批量更新两种形态都没有的合法字面量近邻——
    `onboarding_service.py` 的玩家化身创建
    `Resident(resident_type="player", ...)`。只认 "npc" 是
    `test_reputation_population_scope.py` 的 `_is_npc_constant` 在读侧已经
    立下的同一条判据，不是本文件的新发明。"""
    return isinstance(node, ast.Constant) and node.value == "npc"


def _is_values_call(node: ast.expr) -> bool:
    """`<expr>.values(...)`——SQLAlchemy Core 批量 UPDATE 的写形态
    （`update(Model).values(...)`）。只看方法名 `values`，不追溯 `<expr>`
    具体是不是 `update()` 的返回值：`resident_type` 是本仓不会在别的语境里
    撞见的关键字/字典键，方法名匹配已经足够精确，误报成本（`dict.values()`
    这种同名但零参调用）为零——它没有匹配的关键字或字典参数，天然不会被
    下面两个探测器命中。"""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "values")


def _bulk_update_offenders(tree: ast.AST, label: str) -> list[str]:
    """形态②：`.values(resident_type=<字面量>)` 与
    `.values({"resident_type": <字面量>})`——两个写入口自己用的正是这个调用
    形状，区别只在于它们传的是符号常量，不是字面量。批量 UPDATE 是唯一写
    入口条款里点名的并发对手，这里不收窄到只认 "npc"：任何硬编码字符串顶
    替符号常量喂进批量 UPDATE，都是同一类绕过（`_is_str_literal`，不是
    `_is_npc_literal`）。"""
    offenders = []
    for node in ast.walk(tree):
        if not _is_values_call(node):
            continue
        for kw in node.keywords:
            if kw.arg == "resident_type" and _is_str_literal(kw.value):
                offenders.append(f"{label}:{node.lineno}")
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                continue
            for k, v in zip(arg.keys, arg.values):
                if (isinstance(k, ast.Constant) and k.value == "resident_type"
                        and _is_str_literal(v)):
                    offenders.append(f"{label}:{node.lineno}")
    return offenders


def _construction_offenders(tree: ast.AST, label: str) -> list[str]:
    """形态③：`Resident(resident_type="npc", ...)`——直接把政治权利焊进构造
    调用，绕开 grant_citizenship 的射程防呆（内置阵容检查、玩家化身检查、
    选民下限）。只收窄到 "npc" 字面量（`_is_npc_literal`，不是
    `_is_str_literal`）：app/ 下有一处结构一致但取值不同、合法且不涉及政治
    权利的对照组——`onboarding_service.py` 的玩家化身创建
    `Resident(resident_type="player", ...)`——同一个"字面量 vs 符号引用"
    判据没法把它和一次真正的 "npc" 绕过分开，只能靠取值本身。五个 UGC 站点
    传的是符号引用 `UGC_RESIDENT_TYPE`，按机制（非字面量）天然豁免，不看
    取值就已经通过。

    ⚠️ 已知边界：这条探测器不拦截把 UGC_RESIDENT_TYPE（"resident"）或
    preset（"preset"）手写成字面量的构造——那些取值不在 CIVIC_VOTER_TYPES
    里，不构成政治权利绕过，只是风格问题；本任务的射程是「唯一写入口」条款
    （政治权利变更收敛），不是「所有字面量都要用符号常量」的泛化代码规范。
    """
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "Resident"):
            continue
        for kw in node.keywords:
            if kw.arg == "resident_type" and _is_npc_literal(kw.value):
                offenders.append(f"{label}:{node.lineno}")
    return offenders


def _all_write_offenders(tree: ast.AST, label: str) -> list[str]:
    """三种写形态合一——「唯一写入口」条款的完整判据。"""
    return (_assignment_offenders(tree, label)
            + _bulk_update_offenders(tree, label)
            + _construction_offenders(tree, label))


def _offenders_in_source(source: str, label: str = "<source>") -> list[str]:
    """给 hermetic 单元测试用：喂源码文本，不碰真实文件。"""
    return _all_write_offenders(ast.parse(source, filename=label), label)


def test_only_civic_membership_writes_resident_type():
    """真实仓库扫描：三种写形态合一。**不做文件级豁免**——civic_membership
    .py 能通过纯粹是因为它的两个写入口结构上不落在任何一个探测器的判据里，
    下面 test_civic_membership_itself_needs_no_path_exemption 单独把这句话
    钉成一条可证伪的断言，不靠这条测试的隐式通过。"""
    offenders = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        rel = str(path.relative_to(BACKEND_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        offenders.extend(_all_write_offenders(tree, rel))
    assert offenders == [], (
        "resident_type 只许由 civic_membership 的两个写入口改写（列上没有 "
        f"CHECK，代码是最后一道闸）：{offenders}")


def test_civic_membership_itself_needs_no_path_exemption():
    """2026-07-27 协调者裁定加宽时点名的最高风险点：civic_membership.py 自己
    的两个写入口会不会被自己的守卫误伤？答案是不需要为它开任何文件级豁免
    ——直接扫描该文件本身（不跳过），三个探测器分别返回空，原因是三条纯
    结构事实，与文件路径无关：

    1. 该文件从不直接给 `x.resident_type` 赋值——两次改档位都走
       `update(Resident).values(...)`（在一个 `begin_nested()` SAVEPOINT
       里），没有一处 `ast.Assign`/`AugAssign`/`AnnAssign` 的 target 是
       `resident_type` 属性。
    2. 它的两次 `.values(resident_type=...)` 调用传的是符号引用
       （`CIVIC_MEMBER_TYPE` / `UGC_RESIDENT_TYPE`），不是字面量
       `ast.Constant`——形态②判据要求值必须是字符串字面量。
    3. 它从不构造 `Resident(...)`——只按 id 操作既有行。

    三条都是 AST 节点类型判定（`ast.Assign` target / `ast.Constant` vs
    `ast.Name` / `ast.Call` 的 `func`），不是 `if path == "app/services/
    civic_membership.py"` 这种文件名比较。"""
    path = BACKEND_ROOT / "app" / "services" / "civic_membership.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert _assignment_offenders(tree, "civic_membership.py") == []
    assert _bulk_update_offenders(tree, "civic_membership.py") == []
    assert _construction_offenders(tree, "civic_membership.py") == []


# ── Guard-of-the-guard（2026-07-27 协调者裁定加宽）───────────────────────
#
# 喂源码文本给探测器，不碰真实文件——hermetic，跑得快，且直接钉住"这个探测器
# 认不认识这种句法形状"，与"全仓当下有没有这种代码"（上面那条真实扫描）是两
# 件事。

_EVASIVE_WRITE_SHAPES = {
    # 形态①——既有覆盖面，协调者点名的两种 target 形状作为回归锁一并钉住
    # （_assignment_offenders 不看 target 前缀，保留在这组参数化里防止未来
    # 重构悄悄收窄）。
    "chained-attr-assign": 'obj.attr.resident_type = "npc"\n',
    "subscripted-chained-attr-assign": 'residents[0].resident_type = "npc"\n',
    "multi-target-assign": 'a = b.resident_type = "npc"\n',
    "augassign": 'resident.resident_type += "x"\n',
    "annassign": 'resident.resident_type: str = "npc"\n',
    # 形态②——批量 UPDATE，_bulk_update_offenders 接住。
    "bulk-update-keyword": (
        'update(Resident).where(Resident.id == rid)'
        '.values(resident_type="npc")\n'),
    "bulk-update-dict": (
        'update(Resident).where(Resident.id == rid)'
        '.values({"resident_type": "npc"})\n'),
    # 形态③——直接构造，_construction_offenders 接住。
    "construction-npc-literal": (
        'Resident(resident_type="npc", creator_id=cid)\n'),
    # 形态④——2026-07-27 评审 Important finding：setattr 是属性赋值的函数
    # 等价物，绕开形态①的静态 `.attr = value` 扫描。此刻（本轮红提交）还没
    # 接，应该红。
    "setattr-literal-field-and-value": (
        'setattr(resident, "resident_type", "npc")\n'),
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
    # 两个写入口自己的批量 UPDATE 调用——协调者点名的最高风险点：接入形态②
    # 之后这两条绝不能被误伤，否则 civic_membership.py 自己会被自己的守卫
    # 挡住。豁免机制是值类型（Name 符号引用，不是 ast.Constant 字面量），
    # 不是文件路径——test_civic_membership_itself_needs_no_path_exemption
    # 对同一个文件做了不跳过任何一行的直接扫描，逐条钉住这句话。
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
    # 两个真实先例——通用字段编辑循环，field 是循环变量，不是字面量
    # "resident_type"。形态④的属性名判据要求 2nd 参是字面量，这两个天然
    # 落不进判据里，不需要为它们开任何豁免（app/routers/admin/events.py:105
    # / app/routers/admin/items.py:73 原文照抄，逐字一致）。
    "setattr-loop-variable-field-events-precedent": (
        'setattr(event, field, value)\n'),
    "setattr-loop-variable-field-items-precedent": (
        'setattr(item, field, value)\n'),
    # setattr 传符号引用——与形态②同一套"字面量 vs 符号引用"豁免机制。
    "setattr-symbolic-value": (
        'setattr(resident, "resident_type", CIVIC_MEMBER_TYPE)\n'),
}


@pytest.mark.parametrize("source", _EXEMPT_WRITE_SHAPES.values(),
                        ids=_EXEMPT_WRITE_SHAPES.keys())
def test_guard_does_not_flag_mechanism_exempt_write_shapes(source):
    """这八类『按机制豁免』的写法（两个写入口自己的批量 UPDATE 调用、五个
    UGC 构造站点、admin preset 透传、onboarding 的合法字面量 "player"、既
    有的 `in` 成员测试、不相关的 `.values()` 调用）都不被误报——豁免全部
    落在值的 AST 节点类型上（Name / 非 "npc" 常量 / 无关键字），没有一条
    靠文件路径。"""
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
