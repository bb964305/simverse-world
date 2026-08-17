"""P2-S9: run_live 在开票那一刻挂一条 type="script" 的舞台事件 + civic 接线。

最要紧的那条断言是 test_the_event_is_visible_to_the_crowd_puller:
crowd_service._EVENT_TYPES_WITH_CROWD 是 ("festival","script"),用 "news" 建的事件
active_event_location 一辈子看不见 —— 学院公开课十五天零到访就是这么来的。

第二要紧的是 test_an_aborted_debate_leaves_no_ghost_event:LLM 失败会走
_auto_draw_refund 当场 settled,若照 design 在进入 live 时建事件,就会留下一条指着
死辩论、还要拉三倍人流一小时的幽灵事件。
"""
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS
from app.config import settings
from app.models.resident import Resident
from app.models.world_event import WorldEvent
from app.services import civic_service, crowd_service
from app.services import debate_service as ds
from app.services.world_event_service import _to_dict

DEBATE_SRC = (Path(__file__).resolve().parents[1]
              / "app" / "services" / "debate_service.py")

THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}


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


def _mock_client(text="我方观点更站得住脚。"):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    client.messages.create = AsyncMock(return_value=resp)
    return client


async def _residents(db):
    db.add_all([
        Resident(slug="ann", name="安", creator_id="system", district="cafe",
                 resident_type="npc", status="idle", tile_x=1, tile_y=1),
        Resident(slug="bo", name="波", creator_id="system", district="cafe",
                 resident_type="npc", status="idle", tile_x=2, tile_y=2),
    ])
    await db.commit()


async def _run(db, *, topic="猫和狗谁更好", venue=None, text="我方观点更站得住脚。"):
    await _residents(db)
    d = await ds.create_debate(db, topic, "ann", "bo", venue=venue)
    with patch("app.llm.client.get_client", return_value=_mock_client(text)), \
         patch("app.llm.metering.record_usage", new_callable=AsyncMock):
        await ds.run_live(db, d)
    return d


async def _events(db) -> list[WorldEvent]:
    return (await db.execute(select(WorldEvent))).scalars().all()


# ── 闸关 = 逐字节旧行为 ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_creates_no_world_event(db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    d = await _run(db_session, venue="theater")
    assert d.status == "voting"
    assert await _events(db_session) == []


# ── 闸开 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_on_opens_one_script_event_at_the_venue(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    before = datetime.now(UTC)
    d = await _run(db_session, venue="theater")

    (ev,) = await _events(db_session)
    assert ev.type == "script"          # 不是 "news" —— 这是全段的关键
    assert ev.payload_json["location_id"] == "theater"
    assert ev.payload_json["debate_id"] == d.id
    assert "剧院" in ev.title and "猫和狗谁更好" in ev.title
    assert "安" in ev.description and "波" in ev.description
    window = (ev.ends_at.replace(tzinfo=UTC) if ev.ends_at.tzinfo is None
              else ev.ends_at) - before
    assert 0 < window.total_seconds() <= settings.debate_vote_window_min * 60 + 5


@pytest.mark.anyio
async def test_the_event_is_visible_to_the_crowd_puller(
        db_session, overlay, monkeypatch):
    """这条就是 design §② 那个「公开课的人流拉力从未生效过」缺陷的反面证明。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _run(db_session, venue="theater")

    (ev,) = await _events(db_session)
    assert ev.type in crowd_service._EVENT_TYPES_WITH_CROWD
    assert crowd_service.active_event_location([_to_dict(ev)]) == "theater"


@pytest.mark.anyio
async def test_gate_on_without_a_venue_creates_nothing(db_session, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    await _run(db_session)
    assert await _events(db_session) == []


@pytest.mark.anyio
async def test_gate_on_legacy_row_without_the_declaration_creates_nothing(
        db_session, overlay, monkeypatch):
    """存量 theater 行没有 capabilities 键 —— 未回填就开闸不出事,只是不建事件。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER)
    await _run(db_session, venue="theater")
    assert await _events(db_session) == []


@pytest.mark.anyio
async def test_an_aborted_debate_leaves_no_ghost_event(
        db_session, overlay, monkeypatch):
    """LLM 空辩词 → _auto_draw_refund 当场 settled。事件必须一条都没有,否则就是
    一条指着死辩论、还要拉三倍人流一小时的幽灵。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    d = await _run(db_session, venue="theater", text="")
    assert d.status == "settled" and d.winner == "draw"
    assert await _events(db_session) == []


@pytest.mark.anyio
async def test_a_long_topic_does_not_overflow_the_title_column(
        db_session, overlay, monkeypatch):
    """WorldEvent.title 是 String(200),Debate.topic 是 String(300) —— 真 PG 上不
    截断就是一条 StringDataRightTruncation。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _run(db_session, topic="长" * 300, venue="theater")
    (ev,) = await _events(db_session)
    assert len(ev.title) <= 200


@pytest.mark.anyio
async def test_a_broken_event_write_never_drags_the_debate_into_a_refund(
        db_session, overlay, monkeypatch):
    """世界事件是叙事装饰;一场已经跑完六轮的辩论不能因为它退款。"""
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})

    async def _boom(_debate_id):
        raise RuntimeError("venue lookup exploded")

    monkeypatch.setattr(ds, "_debate_venue", _boom)
    d = await _run(db_session, venue="theater")
    assert d.status == "voting" and d.winner is None
    assert await _events(db_session) == []


# ── civic 接线 ────────────────────────────────────────────────────────

def _lecture_pool(db):
    def _res(slug, name, sbti, **kw):
        # is_autonomous 是只读 hybrid(resident.py:92-106),由 resident_type 派生:
        # "npc" ∈ SIM_RESIDENT_TYPES ⇒ 进 maybe_spawn_lecture_debate 的候选池。
        d = dict(slug=slug, name=name, district="town_hall", status="idle",
                 resident_type="npc", creator_id="sys", tile_x=119, tile_y=53,
                 meta_json={"sbti": {"dimensions": sbti}})
        d.update(kw)
        return Resident(**d)

    db.add_all([
        _res("opt", "乐观者", {"So1": "H", "A1": "H"}),
        _res("skept", "怀疑者", {"So1": "H", "A1": "L"}),
    ])


@pytest.mark.anyio
async def test_lecture_debate_gets_the_stage_venue_when_the_gate_is_on(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", True)
    slug = overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    _lecture_pool(db_session)
    await db_session.commit()

    event = {"title": "小镇的来路的公开课", "payload_json": {"duty": "lecturer"}}
    assert await civic_service.maybe_spawn_lecture_debate(db_session, event) is True

    from app.models.debate import Debate
    d = (await db_session.execute(select(Debate))).scalars().one()
    assert await ds._debate_venue(d.id) == slug


@pytest.mark.anyio
async def test_lecture_debate_gets_no_venue_when_the_gate_is_off(
        db_session, overlay, monkeypatch):
    monkeypatch.setattr(settings, "stage_event_enabled", False)
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    _lecture_pool(db_session)
    await db_session.commit()

    event = {"title": "小镇的来路的公开课", "payload_json": {"duty": "lecturer"}}
    assert await civic_service.maybe_spawn_lecture_debate(db_session, event) is True

    from app.models.debate import Debate
    d = (await db_session.execute(select(Debate))).scalars().one()
    assert await ds._debate_venue(d.id) is None


def test_civic_does_not_hardcode_the_theater_slug():
    """剧院是公投建的动态行,slug 是数据不是代码常量 —— 走能力反查。"""
    src = (Path(__file__).resolve().parents[1]
           / "app" / "services" / "civic_service.py").read_text(encoding="utf-8")
    body = src.split("async def maybe_spawn_lecture_debate", 1)[1].split(
        "\n# ── helper", 1)[0]
    assert '"theater"' not in body and "'theater'" not in body


def test_debate_service_still_has_no_location_column_semantics():
    """零迁移:场地只走 Redis 与 payload,不进 debates 表。"""
    text = DEBATE_SRC.read_text(encoding="utf-8")
    assert "mapped_column" not in text and "add_column" not in text


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
