"""F3 Task 1 — 清理「已离开集合 S 的居民」的扫描不得用 S 本身做 WHERE。

office_service.py 原实现用 ``Resident.is_autonomous`` 圈定要清 meta_json
['mayor'] 的行。这个谓词恰好会漏掉唯一必须清的那个人：任内被降级 / 逐出
/ 本来就是玩家化身的镇长——他掉出 SIM_RESIDENT_TYPES，工资倍率标记于是
永久留在 meta_json 上（gotcha #1，双份工资倍率的来源）。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.models.office import Office
from app.models.resident import Resident
from app.services.office_service import OfficeService


def _res(slug, name, meta=None, rtype="npc"):
    return Resident(
        slug=slug, name=name, district="central_plaza", status="idle",
        resident_type=rtype, creator_id="sys", tile_x=70, tile_y=56,
        meta_json=meta,
    )


@pytest.mark.anyio
async def test_vacate_clears_mayor_flag_for_non_autonomous_holder(db_session):
    """离任者已不在人口集合内（player）——仍然必须被清掉工资倍率标记。"""
    holder = _res("ex-mayor", "前镇长", meta={"mayor": True}, rtype="player")
    db_session.add(holder)
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "ex-mayor", fill_strategy="election")
    assert await svc.vacate("mayor") is True

    await db_session.refresh(holder)
    assert not (holder.meta_json or {}).get("mayor")


@pytest.mark.anyio
async def test_vacate_residual_sweep_has_no_membership_predicate(db_session):
    """残留扫描的唯一谓词是 meta_json IS NOT NULL：另一个挂着陈年 mayor
    标记、类型为 preset（既不在 CIVIC_VOTER_TYPES 也不在 SIM_RESIDENT_TYPES）
    的居民同样要被清掉。"""
    stale = _res("stale-flag", "陈年标记", meta={"mayor": True}, rtype="preset")
    sitting = _res("sitting", "在任", meta={"mayor": True})
    db_session.add_all([stale, sitting])
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "sitting", fill_strategy="election")
    assert await svc.vacate("mayor") is True

    await db_session.refresh(stale)
    await db_session.refresh(sitting)
    assert not (stale.meta_json or {}).get("mayor")
    assert not (sitting.meta_json or {}).get("mayor")


@pytest.mark.anyio
async def test_term_check_clears_flag_for_non_autonomous_holder(db_session):
    """term_check 走的是同一个清理入口，同样按 slug 直查离任者。"""
    holder = _res("termed-out", "任满", meta={"mayor": True}, rtype="player")
    db_session.add(holder)
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "termed-out", fill_strategy="election", term_days=7)
    assert await svc.term_check(now=datetime.now(UTC) + timedelta(days=365)) == 1

    await db_session.refresh(holder)
    assert not (holder.meta_json or {}).get("mayor")
    assert (await db_session.execute(
        select(Office.holder_slug).where(Office.office_key == "mayor")
    )).scalar_one() is None


@pytest.mark.anyio
async def test_vacate_still_nulls_system_config_current_mayor(db_session):
    """回归：slug 直查改造不得弄丢 system_config 回落值的清理。"""
    from app.services.config_service import ConfigService

    db_session.add(_res("old", "老镇长", meta={"mayor": True}))
    await db_session.commit()
    await ConfigService(db_session).set(
        "current_mayor", "old", group="civic", updated_by="test")

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "old", fill_strategy="election")
    assert await svc.vacate("mayor") is True
    assert await ConfigService(db_session).get("current_mayor") is None


@pytest.mark.anyio
async def test_vacate_clears_mayor_flag_when_gate_on(db_session, monkeypatch):
    """约束 2 补丁：gate=on 态下同样要断言行为，不能只断言「路径被执行到」。

    离任者用非自治 resident_type（player）构造——修复点正是「离任者已掉出
    自治集合」这个场景，用 npc 造不出这个缺口，即使把 gate 打开也测不到。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "polis_office_enabled", True)

    holder = _res("gate-on-mayor", "gate开镇长", meta={"mayor": True}, rtype="player")
    db_session.add(holder)
    await db_session.commit()

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "gate-on-mayor", fill_strategy="election")
    assert await svc.vacate("mayor") is True

    await db_session.refresh(holder)
    assert not (holder.meta_json or {}).get("mayor")
