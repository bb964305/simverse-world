"""P2-S15: 辩论场地反查 + 在场观众名单(纯查询,零生产调用方,不挂闸)。

核心是一条与邮局侧同构的校正:在场判定必须走 capability_location_at,不能走
get_location_id_at —— theater(172,40,178,50) 完全落在 outdoor 街区
east_gardens(140,35,179,58) 内部。
test_masking_is_real_and_the_audience_sees_through_it 同时钉死「遮蔽是真的」与
「能力反查穿透」两件事。
"""
import pytest

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS, get_location_id_at
from app.models.resident import Resident
from app.models.world_event import WorldEvent
from app.services import debate_service as ds

# 生产 dynamic_locations 里 theater 那行的 data_json(civic_service.py:188-193 原文)。
# capabilities 由调用方按场景决定加不加 —— 存量行今天**没有**这个键。
THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:说书、演展、故事会的舞台",
    "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
}
INSIDE = (175, 45)
OUTSIDE = (75, 56)


@pytest.fixture
def overlay():
    """模拟 load_dynamic_locations 的合入:追加到 LOCATIONS 尾部,再还原。"""
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


async def _resident(db, slug, tile=INSIDE, *, status="idle", rtype="npc"):
    r = Resident(slug=slug, name=slug, creator_id="system", district="cafe",
                 status=status, resident_type=rtype,
                 tile_x=tile[0], tile_y=tile[1])
    db.add(r)
    await db.commit()
    return r


async def _debate(db):
    await _resident(db, "ann", OUTSIDE)
    await _resident(db, "bo", OUTSIDE)
    return await ds.create_debate(db, "猫和狗谁更好", "ann", "bo")


async def _script_event(db, debate_id, venue="theater"):
    ev = WorldEvent(type="script", title="辩论", description="",
                    payload_json={"location_id": venue, "debate_id": debate_id})
    db.add(ev)
    await db.commit()
    return ev


# ── stage_venue_of ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_venue_is_read_from_the_script_event(db_session):
    """payload 契约由批次表 #7 定:type=\"script\" + {location_id, debate_id}。"""
    d = await _debate(db_session)
    await _script_event(db_session, d.id)
    assert await ds.stage_venue_of(db_session, d.id) == "theater"


@pytest.mark.anyio
async def test_venue_is_none_without_the_event(db_session):
    """今天世界里每一场辩论都是这个形态 —— 降级即今天的行为。"""
    d = await _debate(db_session)
    assert await ds.stage_venue_of(db_session, d.id) is None


@pytest.mark.anyio
async def test_venue_ignores_other_debates_and_other_event_types(db_session):
    d = await _debate(db_session)
    await _script_event(db_session, "some-other-debate")
    db_session.add(WorldEvent(type="news", title="公开课", description="",
                              payload_json={"location_id": "academy",
                                            "debate_id": d.id}))
    await db_session.commit()
    assert await ds.stage_venue_of(db_session, d.id) is None


@pytest.mark.anyio
async def test_venue_tolerates_a_malformed_payload(db_session):
    d = await _debate(db_session)
    db_session.add(WorldEvent(type="script", title="坏行", description="",
                              payload_json={"debate_id": d.id}))
    await db_session.commit()
    assert await ds.stage_venue_of(db_session, d.id) is None


# ── stage_audience ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_masking_is_real_and_the_audience_sees_through_it(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    # 遮蔽是真的:首命中返回的是 outdoor 街区,不是剧院。
    assert get_location_id_at(*INSIDE) == "east_gardens"
    await _resident(db_session, "watcher", INSIDE)
    seats = await ds.stage_audience(db_session, "theater", seed="d1")
    assert [r.slug for r in seats] == ["watcher"]


@pytest.mark.anyio
async def test_people_outside_the_venue_are_not_audience(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _resident(db_session, "passerby", OUTSIDE)
    assert await ds.stage_audience(db_session, "theater", seed="d1") == []


@pytest.mark.anyio
async def test_legacy_row_without_the_declaration_is_inert(db_session, overlay):
    """存量 dynamic_locations 行没有 capabilities 键 —— 未回填时名单必须为空,
    绝不能抛,也绝不能瞎认。"""
    overlay("theater", THEATER)
    await _resident(db_session, "watcher", INSIDE)
    assert await ds.stage_audience(db_session, "theater", seed="d1") == []


@pytest.mark.anyio
async def test_sleepers_and_non_sim_residents_are_excluded(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _resident(db_session, "sleeper", INSIDE, status="sleeping")
    await _resident(db_session, "ugc", INSIDE, rtype="character")
    assert await ds.stage_audience(db_session, "theater", seed="d1") == []


@pytest.mark.anyio
async def test_debaters_are_not_their_own_audience(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _resident(db_session, "ann", INSIDE)
    await _resident(db_session, "watcher", INSIDE)
    seats = await ds.stage_audience(db_session, "theater", seed="d1",
                                    exclude_slugs=("ann", "bo"))
    assert [r.slug for r in seats] == ["watcher"]


@pytest.mark.anyio
async def test_audience_is_capped_and_deterministic(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    for i in range(12):
        await _resident(db_session, f"w{i}", INSIDE)
    first = [r.slug for r in await ds.stage_audience(db_session, "theater", seed="d1")]
    again = [r.slug for r in await ds.stage_audience(db_session, "theater", seed="d1")]
    assert len(first) == ds.AUDIENCE_LIMIT == 8
    assert first == again           # 同 seed 同名单
    other = [r.slug for r in await ds.stage_audience(db_session, "theater", seed="d2")]
    assert set(other) <= {f"w{i}" for i in range(12)}


def test_action_type_enum_is_untouched():
    """P2 全段零新增 ActionType(design_P2.md §「为什么不新增 ActionType」)。"""
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
