"""P2-S11: stage 事件场地解析 —— 能力门 + 可达性自保 + 稳定排序 helper 收敛。

三条判据各防一类事故:
  · 能力门:场地资格来自地点自己的 capabilities 声明(CAP_STAGE),不是 slug 字面量。
    这是 §③ 路 B 的机器表述 —— 拉力与在场人数解耦,actions.py 的 CHAT_RESIDENT
    判据一个字不改。
  · 可达性门:get_valid_target_tile 返回的 tile 必须在 get_reachable_tiles() 里。
    pathfinder._get_forced_walkable(:60-68)无边界检查地把每个地点的 entrance/center
    强标 walkable,所以 walkable 会自证成功(实测 theater center(175,45)
    walkable=True / reachable=False)。
  · 同串收敛:_stable_market_rank 委托给 _stable_rank 后必须逐字节等价。

文件名注:计划里本 step 与 P2-S8 撞名(两处都写 tests/test_stage_event_venue.py),
S8 先落地占了那个名字,本 step 改用 test_crowd_stage_venue.py —— 被测模块是
crowd_service,且本文件还覆盖与 stage 无关的 _stable_rank 收敛。
"""
import hashlib

import pytest

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS, get_location_id_at
from app.agent import pathfinder
from app.services import crowd_service

# 生产 dynamic_locations 里 theater 那行的 data_json(2026-08 公投建,active=t)。
# capabilities 由调用方按场景决定加不加 —— 存量行今天**没有**这个键。
THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:辩论、公开课、戏与人群",
    "boosted_actions": ["OBSERVE", "CHAT_RESIDENT"],
}
#: entrance 落在孤岛上的假场地(x=175 在 theater bounds 内但不与镇区连通)。
ISLAND_STAGE = {**THEATER, "name": "孤岛戏台", "entrance": (175, 45)}


def _script(location_id, *, etype="script", eid="stage-1"):
    return {
        "id": eid, "type": etype, "title": "一场辩论",
        "starts_at": "2026-08-17T10:00:00+00:00",
        "ends_at": "2026-08-17T11:00:00+00:00",
        "payload_json": {"location_id": location_id, "debate_id": "d1"},
    }


SEASON_SCRIPT = {
    "id": "s1", "type": "script", "title": "剧本 · 第1幕",
    "starts_at": "", "ends_at": "",
    "payload_json": {"season_id": "se1", "act": 1},
}
MARKET_DAY = {
    "id": "market-1", "type": "festival", "title": "集市日",
    "starts_at": "2026-08-13T00:00:00+00:00",
    "ends_at": "2026-08-14T00:00:00+00:00",
    "payload_json": {"market_day": True, "location_id": "market_hall"},
}
LECTURE_NEWS = {
    "id": "n1", "type": "news", "title": "顾明远的公开课",
    "starts_at": "", "ends_at": "",
    "payload_json": {"location_id": "academy", "duty": "lecturer"},
}


@pytest.fixture
def overlay():
    """模拟 load_dynamic_locations 的合入:追加到 LOCATIONS 尾部,再还原。

    合入/摘除都要重置 pathfinder 缓存 —— get_walkable_tiles 会 force-add 每个地点的
    entrance/center(pathfinder.py:94-96),LOCATIONS 变了缓存就过期。
    """
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
        assert slug not in LOCATIONS, slug
        row = dict(data)
        if capabilities is not None:
            row["capabilities"] = capabilities
        LOCATIONS[slug] = row
        added.append(slug)
        pathfinder.reset_walkable_cache()
        return slug

    yield _merge
    for slug in added:
        LOCATIONS.pop(slug, None)
    pathfinder.reset_walkable_cache()


# ── 稳定排序 helper 的同串收敛 ────────────────────────────────────────

def test_stable_rank_is_plain_sha256_over_a_unit_separated_material():
    assert crowd_service._stable_rank(("a", "b", "c", "d"), "r1") == \
        hashlib.sha256("a\x1fb\x1fc\x1fd\x1fr1".encode("utf-8")).digest()


def test_market_rank_is_byte_identical_after_delegating():
    """集市 cohort 的选人结果不得因为本次收敛发生一位比特的变化。"""
    key = ("market-1", "2026-08-13T00:00:00+00:00", "2026-08-14T00:00:00+00:00")
    for rid in ("r1", "r2", "骆小舟"):
        assert crowd_service._stable_market_rank(key, rid) == \
            hashlib.sha256("\x1f".join((*key, rid)).encode("utf-8")).digest()


# ── 能力门 ────────────────────────────────────────────────────────────

def test_no_events_no_venue():
    assert crowd_service.stage_event_venue([]) is None
    assert crowd_service.stage_event_venue(None) is None
    assert crowd_service.active_stage_event_id([]) is None


def test_a_script_event_at_a_stage_capable_venue_is_recognized(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    events = [_script("theater")]
    assert crowd_service.stage_event_venue(events) == "theater"
    assert crowd_service.active_stage_event_id(events) == "stage-1"


def test_a_venue_without_the_stage_declaration_is_inert(overlay):
    """存量 dynamic_locations 行没有 capabilities 键 —— #7 的声明未落地时本段整段
    不生效,而不是乱拉人。缺省安全。"""
    overlay("theater", THEATER)
    assert crowd_service.stage_event_venue([_script("theater")]) is None


def test_an_unknown_location_id_is_ignored(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([_script("nowhere_at_all")]) is None
    assert crowd_service.stage_event_venue([_script(None)]) is None
    assert crowd_service.stage_event_venue([_script(123)]) is None


def test_market_day_is_not_a_stage_event(overlay):
    """market_hall 不声明 stage —— 集市 cohort 与观众 cohort 不得互相抢人。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([MARKET_DAY]) is None


def test_season_script_events_carry_no_location_and_stay_inert(overlay):
    """script_service.py:79-87 建的季节剧本 payload 只有 season_id/act。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([SEASON_SCRIPT]) is None


def test_a_news_typed_event_is_never_picked_up(overlay):
    """type="news" 的行不得长出人流语义 —— NEWS_POOL 的随机新闻也是 news。

    P2-S10 开闸后公开课已改发 type="script",故这条钉的不再是「公开课的现状」而是
    类型门本身:只有 _EVENT_TYPES_WITH_CROWD 里的 type 能解析出场地。那个元组的内容
    由 tests/test_stage_event_lecture.py::test_news_never_gains_crowd_semantics 单独
    看守,这里不重复登记。
    """
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([LECTURE_NEWS]) is None
    assert crowd_service.stage_event_venue(
        [{**LECTURE_NEWS, "payload_json": {"location_id": "theater"}}]) is None


def test_a_festival_at_a_stage_venue_also_counts(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue(
        [_script("theater", etype="festival")]) == "theater"


# ── 可达性自保 ────────────────────────────────────────────────────────

def test_an_island_venue_is_refused(overlay):
    """entrance 不与镇区连通 → 不认这个场地。否则名单里的人每 tick 走一条
    find_path 恒返 None 的路线,arrivals 永远 0 而日行动 cap 被烧光。"""
    overlay("island_stage", ISLAND_STAGE, capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([_script("island_stage")]) is None


def test_the_production_theater_entrance_is_reachable_today(overlay):
    """本段**不依赖** P3-c 的 bounds 迁移:生产 theater 的 entrance(172,45)今天就是
    连通的,只有 center(175,45)是孤岛,而 get_valid_target_tile 有 entrance 就永不取
    center(map_data.py:652-657)。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    reachable = pathfinder.get_reachable_tiles()
    assert (172, 45) in reachable
    assert (175, 45) not in reachable
    assert crowd_service.stage_event_venue([_script("theater")]) == "theater"


def test_a_venue_without_a_target_tile_is_refused(overlay):
    """data_json 写了 "entrance": null 时 get_valid_target_tile 返 None,不回退
    center(map_data.py:657 是 .get 的默认值形式)。"""
    overlay("no_door", {**THEATER, "entrance": None, "center": None},
            capabilities={CAP_STAGE: {}})
    assert crowd_service.stage_event_venue([_script("no_door")]) is None


# ── 站位反查穿透遮蔽 ─────────────────────────────────────────────────

def test_stage_venue_at_pierces_the_outdoor_mask(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    # 遮蔽是真的:首命中返回的是 outdoor 街区,不是剧院。
    assert get_location_id_at(174, 45) == "east_gardens"
    assert get_location_id_at(172, 45) == "east_gardens"
    # 能力反查穿透遮蔽。
    assert crowd_service.stage_venue_at(174, 45) == "theater"
    assert crowd_service.stage_venue_at(172, 45) == "theater"
    # 镇中心不在任何 stage 地点里。
    assert crowd_service.stage_venue_at(75, 56) is None


def test_stage_venue_at_is_none_without_the_declaration(overlay):
    overlay("theater", THEATER)
    assert crowd_service.stage_venue_at(174, 45) is None


def test_action_type_enum_is_untouched():
    """P2 全段零新增 ActionType(design_P2.md §「为什么不新增 ActionType」)。"""
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
