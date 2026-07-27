"""F2 Task 10 —— resident_type 收敛为唯一写入口。

列上没有 CHECK（app/models/resident.py:55 是裸 String(20)），代码就是最后
一道闸。admin/residents.py:117-118 是仓库里唯一的运行时裸赋值，也是 F2 批量
UPDATE 唯一的并发对手（正面样板：relation_service.py:214-223；反面样板：
admin/residents.py:103-127 的读-改-写）。

结构性守卫仿 tests/test_ugc_resident_no_political_rights.py:69-88 的 AST 扫描，
把覆盖面从「Resident(...) 构造」扩展到「*.resident_type = ...」赋值。
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


def test_only_civic_membership_assigns_resident_type():
    offenders = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        rel = str(path.relative_to(BACKEND_ROOT))
        if rel in _ASSIGNMENT_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "resident_type":
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        "resident_type 只许由 civic_membership 的两个写入口改写（列上没有 "
        f"CHECK，代码是最后一道闸）：{offenders}")


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
