"""P2-S10: 公开课事件的 type 从 "news" 改 "script" —— 修「人流拉力从未生效过」。

本文件的核心是一对对照:同一段代码,闸关时建出来的事件
active_event_location 看不见,闸开时看得见。这就是 design_P2.md §② 那个新发现
缺陷的可执行形态。

第二条是冷却:闸是可以来回翻的,而冷却窗口是 7 天。查询只认一种 type 的话,翻闸
当天(任一方向)冷却当场失效,讲师每次 WORK 都开一场新课。
"""
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.map_data import LOCATIONS
from app.config import settings
from app.models.resident import Resident
from app.models.world_event import WorldEvent
from app.services import crowd_service, duty_service
from app.services.world_event_service import _to_dict

DUTY_SRC = (Path(__file__).resolve().parents[1]
            / "app" / "services" / "duty_service.py")


def _lecturer() -> Resident:
    return Resident(
        slug="gu", name="顾明远", creator_id="sys", resident_type="npc",
        district="academy", status="idle", tile_x=70, tile_y=56,
        meta_json={"duty": {"key": "lecturer",
                            "perks": {"lecture_cooldown_days": 7}}},
    )


async def _lecture(db) -> Resident:
    r = _lecturer()
    db.add(r)
    await db.commit()
    return r


async def _events(db) -> list[WorldEvent]:
    return (await db.execute(select(WorldEvent))).scalars().all()


# ── 闸关 = 逐字节旧行为 ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_still_writes_a_news_event(db_session, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    r = await _lecture(db_session)

    line = await duty_service._work_lecturer(db_session, r)

    assert line == "顾明远在学院挂出了公开课的讲题"
    (ev,) = await _events(db_session)
    assert ev.type == "news"
    assert ev.title == "顾明远的公开课"
    assert ev.payload_json == {"location_id": "academy", "duty": "lecturer"}


@pytest.mark.anyio
async def test_gate_off_event_is_invisible_to_the_crowd_puller(
        db_session, monkeypatch):
    """这就是缺陷本身:payload 里写着 academy,拉力却一次都没生效过。"""
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    r = await _lecture(db_session)
    await duty_service._work_lecturer(db_session, r)

    (ev,) = await _events(db_session)
    assert ev.type not in crowd_service._EVENT_TYPES_WITH_CROWD
    assert crowd_service.active_event_location([_to_dict(ev)]) is None


# ── 闸开 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_on_writes_a_script_event_with_the_same_payload(
        db_session, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    r = await _lecture(db_session)

    line = await duty_service._work_lecturer(db_session, r)

    assert line == "顾明远在学院挂出了公开课的讲题"   # 叙事一个字不变
    (ev,) = await _events(db_session)
    assert ev.type == "script"
    assert ev.title == "顾明远的公开课"
    assert ev.payload_json == {"location_id": "academy", "duty": "lecturer"}


@pytest.mark.anyio
async def test_gate_on_event_finally_pulls_a_crowd(db_session, monkeypatch):
    """修好之后的正面证明。academy 是静态地点,恒在 LOCATIONS 里。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    assert "academy" in LOCATIONS
    r = await _lecture(db_session)
    await duty_service._work_lecturer(db_session, r)

    (ev,) = await _events(db_session)
    assert ev.type in crowd_service._EVENT_TYPES_WITH_CROWD
    assert crowd_service.active_event_location([_to_dict(ev)]) == "academy"


# ── 冷却跨闸 ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_cooldown_survives_flipping_the_gate_on(db_session, monkeypatch):
    """旧课是 "news",翻闸后查询只认 "script" 的话冷却当场失效。"""
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    r = await _lecture(db_session)
    assert await duty_service._work_lecturer(db_session, r) is not None

    monkeypatch.setattr(settings, "stage_event_enabled", True)
    assert await duty_service._work_lecturer(db_session, r) is None
    assert len(await _events(db_session)) == 1


@pytest.mark.anyio
async def test_cooldown_survives_flipping_the_gate_back_off(
        db_session, monkeypatch):
    """回滚方向同样成立 —— 闸是可以随时翻回去的。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    r = await _lecture(db_session)
    assert await duty_service._work_lecturer(db_session, r) is not None

    monkeypatch.setattr(settings, "stage_event_enabled", False)
    assert await duty_service._work_lecturer(db_session, r) is None
    assert len(await _events(db_session)) == 1


def test_the_cooldown_type_set_is_declared_once_and_covers_both():
    assert duty_service._LECTURE_EVENT_TYPES == ("news", "script")


# ── 边界守卫 ──────────────────────────────────────────────────────────

def test_news_never_gains_crowd_semantics():
    """不许图省事把 "news" 塞进那个元组:NEWS_POOL 的四条随机新闻会跟着长出人流
    语义,全镇往一条「今天风很大」的新闻里跑。"""
    assert crowd_service._EVENT_TYPES_WITH_CROWD == ("festival", "script")


def test_no_bare_news_literal_left_in_the_lecturer_handler():
    """type 与冷却判据必须来自同一处声明,不得各自手写字面量。"""
    text = DUTY_SRC.read_text(encoding="utf-8")
    body = text.split("async def _work_lecturer", 1)[1].split(
        "async def _work_researcher", 1)[0]
    offenders = [line.strip() for line in body.splitlines()
                 if not line.lstrip().startswith("#") and '"news"' in line
                 and "_LECTURE_EVENT_TYPES" not in line
                 and "stage_event_enabled" not in line]
    assert not offenders, offenders


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
