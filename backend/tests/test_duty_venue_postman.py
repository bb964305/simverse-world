"""P2-S3: _work_postman 的现场分支、metadata['duty'],以及胶囊的向后兼容硬清单。

三条硬约束在本文件里是可执行断言,不是注释:
  1 投递的合法性与地点无关 —— 三种闸/位置组合下,到期的 sealed 胶囊都必须被送达;
  2 封存/领取都不得改成「必须在邮局」 —— capsule_service 全文不得出现地点语义,
    两个公开函数的签名不得多出地点参数;
  3 闸关 = 逐字节旧行为 —— 记忆文本逐字相等,且不写任何 metadata['duty'],
    feed payload 不多键。
"""
import inspect
import re
from datetime import datetime, timedelta, UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_POSTAL
from app.agent.map_data import LOCATIONS
from app.config import Settings, settings
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.time_capsule import TimeCapsule
from app.models.user import User
from app.services import capsule_service, duty_service

BACKEND = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = BACKEND / ".env.example"
CAPSULE_SRC = BACKEND / "app" / "services" / "capsule_service.py"
NIGHTLY_SRC = BACKEND / "app" / "tasks" / "nightly_cron.py"

POST_OFFICE = {
    "name": "邮局", "type": "public", "role": "logistics",
    "bounds": (44, 100, 48, 106), "center": (46, 103), "entrance": (46, 100),
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"],
}

LEGACY_NOTE_DELIVERED = "今天送到了 1 封到期的信,看着收信的人拆开,值了。"
LEGACY_NOTE_IDLE = "今天把该走的路线跑了一遍,没有迟到的信。"


@pytest.fixture
def overlay():
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
        assert slug not in LOCATIONS, slug
        row = dict(data)
        if capabilities is not None:
            row["capabilities"] = capabilities
        LOCATIONS[slug] = row
        added.append(slug)
        return slug

    yield _merge
    for slug in added:
        LOCATIONS.pop(slug, None)


async def _postman_row(db, tile=(0, 0)) -> Resident:
    r = Resident(id="post-1", slug="luo-xiaozhou", name="骆小舟", creator_id="system",
                 resident_type="npc", district="south_quarter", status="idle",
                 tile_x=tile[0], tile_y=tile[1],
                 meta_json={"duty": {"key": "postman"}})
    db.add(r)
    await db.commit()
    return r


async def _overdue_capsule(db) -> TimeCapsule:
    u = User(name="u", email="venue@t.com", soul_coin_balance=100)
    db.add(u)
    await db.commit()
    c = TimeCapsule(user_id=u.id, carrier_resident_slug="luo-xiaozhou",
                    deliver_on=datetime.now(UTC).date() - timedelta(days=2),
                    content="到期的信", status="sealed")
    db.add(c)
    await db.commit()
    return c


async def _run_postman(db, resident):
    with patch("app.services.notification_service.manager.is_online",
               AsyncMock(return_value=False)):
        return await duty_service._work_postman(db, resident)


async def _only_memory(db) -> Memory:
    rows = (await db.execute(select(Memory))).scalars().all()
    assert len(rows) == 1, rows
    return rows[0]


# ── 闸本身 ────────────────────────────────────────────────────────────

def test_flag_defaults_to_off():
    assert Settings.model_fields["duty_venue_enabled"].default is False


def test_flag_is_documented_as_false_in_backend_env_example():
    assert "DUTY_VENUE_ENABLED=false" in ENV_EXAMPLE.read_text(encoding="utf-8")


# ── 闸关 = 逐字节旧行为 ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_keeps_the_legacy_note_and_writes_no_duty_metadata(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "duty_venue_enabled", False)
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = await _postman_row(db_session, tile=(46, 103))   # 就站在邮局里
    await _overdue_capsule(db_session)

    line = await _run_postman(db_session, r)

    assert line == "骆小舟跑完了今天的投递(送达 1 封)"
    mem = await _only_memory(db_session)
    assert mem.content == LEGACY_NOTE_DELIVERED
    assert "duty" not in (mem.metadata_json or {})


@pytest.mark.anyio
async def test_gate_off_idle_note_is_byte_identical(db_session, monkeypatch):
    monkeypatch.setattr(settings, "duty_venue_enabled", False)
    r = await _postman_row(db_session)
    await _run_postman(db_session, r)
    assert (await _only_memory(db_session)).content == LEGACY_NOTE_IDLE


# ── 闸开:不在现场 = 老叙事 + 统计标记 ────────────────────────────────

@pytest.mark.anyio
async def test_gate_on_off_site_keeps_the_legacy_note_but_records_at_null(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "duty_venue_enabled", True)
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = await _postman_row(db_session, tile=(75, 56))    # 镇中心,不在邮局
    await _overdue_capsule(db_session)

    await _run_postman(db_session, r)

    mem = await _only_memory(db_session)
    assert mem.content == LEGACY_NOTE_DELIVERED          # 叙事不变
    assert mem.metadata_json["duty"] == {
        "key": "postman", "at": None, "delivered": 1}    # 但分母进了统计


# ── 闸开:在现场 ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_on_on_site_records_the_venue_and_names_it(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "duty_venue_enabled", True)
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = await _postman_row(db_session, tile=(46, 103))
    await _overdue_capsule(db_session)

    await _run_postman(db_session, r)

    mem = await _only_memory(db_session)
    assert mem.metadata_json["duty"] == {
        "key": "postman", "at": "post_office", "delivered": 1}
    assert "邮局" in mem.content        # 地点显示名,不是硬编码 slug
    assert mem.content != LEGACY_NOTE_DELIVERED


@pytest.mark.anyio
async def test_gate_on_legacy_row_without_declaration_degrades_to_off_site(
        db_session, overlay, monkeypatch):
    """存量 dynamic_locations 行没有 capabilities 键 —— 未回填就开闸不出事,
    只是 at 恒为 None(与今天等价)。"""
    monkeypatch.setattr(settings, "duty_venue_enabled", True)
    overlay("post_office", POST_OFFICE)
    r = await _postman_row(db_session, tile=(46, 103))
    await _overdue_capsule(db_session)

    await _run_postman(db_session, r)

    mem = await _only_memory(db_session)
    assert mem.content == LEGACY_NOTE_DELIVERED
    assert mem.metadata_json["duty"]["at"] is None


@pytest.mark.anyio
async def test_realism_raw_importance_and_duty_metadata_coexist(
        db_session, overlay, monkeypatch):
    """add_memory 在 realism 开时会往 metadata 里塞 raw_importance
    (memory/service.py:120-123)——两个键必须共存,不能互相覆盖。"""
    monkeypatch.setattr(settings, "duty_venue_enabled", True)
    monkeypatch.setattr(settings, "realism_enabled", True)
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = await _postman_row(db_session, tile=(46, 103))

    await _run_postman(db_session, r)

    meta = (await _only_memory(db_session)).metadata_json
    assert meta["duty"]["at"] == "post_office"
    assert "raw_importance" in meta


# ── 硬门:存量胶囊在任何组合下都不得失效 ──────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("gate,tile", [
    (False, (46, 103)), (True, (46, 103)), (True, (75, 56)), (True, (0, 0)),
])
async def test_overdue_capsules_are_always_delivered(
        db_session, overlay, monkeypatch, gate, tile):
    """M2′ 护栏的单测形态:任何闸态 / 任何站位下,到期 sealed 胶囊必须清零。"""
    monkeypatch.setattr(settings, "duty_venue_enabled", gate)
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = await _postman_row(db_session, tile=tile)
    cap = await _overdue_capsule(db_session)

    await _run_postman(db_session, r)

    await db_session.refresh(cap)
    assert cap.status == "delivered" and cap.delivered_at is not None
    overdue_sealed = (await db_session.execute(
        select(TimeCapsule).where(TimeCapsule.status == "sealed")
    )).scalars().all()
    assert overdue_sealed == []


# ── 向后兼容硬清单 ───────────────────────────────────────────────────

def test_capsule_service_has_no_location_semantics_at_all():
    """封存/领取都不得改成「必须在邮局」——邮局是投递现场,不是准入条件。"""
    text = CAPSULE_SRC.read_text(encoding="utf-8")
    hits = re.findall(r"location|venue|capabilit|tile_[xy]|duty_venue", text, re.I)
    assert hits == [], hits


def test_capsule_public_signatures_are_frozen():
    assert list(inspect.signature(capsule_service.create_capsule).parameters) == [
        "db", "user_id", "carrier_slug", "deliver_on", "content"]
    assert list(inspect.signature(capsule_service.deliver_due_capsules).parameters) == [
        "db", "today"]


def test_deliver_where_clause_has_no_location_condition():
    src = inspect.getsource(capsule_service.deliver_due_capsules)
    assert "TimeCapsule.deliver_on <= today, TimeCapsule.status == \"sealed\"" in src
    assert "location" not in src and "venue" not in src


def test_nightly_cron_keeps_the_unconditional_fallback():
    text = NIGHTLY_SRC.read_text(encoding="utf-8")
    assert "n = await deliver_due_capsules(db)" in text
    assert "duty_venue" not in text and "capabilit" not in text


def test_time_capsule_columns_are_unchanged():
    assert set(TimeCapsule.__table__.columns.keys()) == {
        "id", "user_id", "carrier_resident_slug", "deliver_on", "content",
        "resident_note", "status", "created_at", "delivered_at"}


def test_serialize_contract_is_append_only_and_unchanged():
    c = SimpleNamespace(
        id="c1", carrier_resident_slug="luo-xiaozhou", deliver_on="2026-09-01",
        status="sealed", content="x", resident_note=None,
        delivered_at=None, created_at=None)
    assert set(capsule_service.serialize(c, include_content=True)) == {
        "id", "carrier_resident_slug", "deliver_on", "status", "content",
        "resident_note", "delivered_at", "created_at"}


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
