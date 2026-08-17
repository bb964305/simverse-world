"""P2-S4: decide 的 _maybe_duty_venue 分支 —— 插在 crowd 之后、Case 2 之前。

三类断言:
  1 分支自身的守卫(闸/可用集/status/粘性行程/GO_HOME/已上工/已在现场);
  2 命中后的上下文副作用(plan_followed=False + plan.status=interrupted),
    漏置会让 tick.py:127-131 把这次自由移动误判成 planned_move 写进粘性行程;
  3 排序不变式:临界需求仍排在本分支之前(0809 死锁的守卫),caravan 的 market
    cohort 仍压过本分支(gameplay 权威),且源码顺序被文本断言钉死。
"""
import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_POSTAL
from app.agent.map_data import LOCATIONS
from app.agent.plan_target import resolve_location_id
from app.agent.schemas import HourlyPlan, TickContext
from app.config import settings
from app.redis_client import get_redis
from app.services import crowd_service, duty_service

DECIDE_SRC = (Path(__file__).resolve().parents[1]
              / "app" / "agent" / "phases" / "decide" / "basic.py")

POST_OFFICE = {
    "name": "邮局", "type": "public", "role": "logistics",
    "bounds": (44, 100, 48, 106), "center": (46, 103), "entrance": (46, 100),
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"],
}
MARKET_DAY = {
    "id": "market-1", "type": "festival", "title": "集市日",
    "starts_at": "2026-08-13T00:00:00+00:00",
    "ends_at": "2026-08-14T00:00:00+00:00",
    "payload_json": {"market_day": True, "location_id": "market_hall"},
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


@pytest.fixture(autouse=True)
def _quiet_world(monkeypatch):
    """默认关掉会抢在本分支之前的两条通路,单测只留一个变量。"""
    monkeypatch.setattr(settings, "duty_venue_enabled", True)
    monkeypatch.setattr(settings, "realism_crowd_enabled", False)
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", False)


def _postman(tile=(75, 56), *, duty_key="postman", status="idle", needs=None):
    meta = {}
    if duty_key:
        meta["duty"] = {"key": duty_key}
    if needs:
        meta["needs"] = needs
    return SimpleNamespace(
        id="post-1", slug="luo-xiaozhou", name="骆小舟", resident_type="npc",
        status=status, tile_x=tile[0], tile_y=tile[1], meta_json=meta,
        home_location_id=None, home_tile_x=5, home_tile_y=5,
    )


def _ctx(resident, world_events=None, plan=None):
    ctx = TickContext(db=AsyncMock(), resident=resident, world_time="10:00",
                      hour=10, schedule_phase="上午",
                      current_plan=plan, scheduled_plan=plan)
    ctx.world_events = world_events or []
    ctx.available_actions = [ActionType.VISIT_DISTRICT, ActionType.WORK,
                             ActionType.IDLE]
    return ctx


def _plugin(**params):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    plug = BasicDecidePlugin(params=params or None)
    plug._load_memories = AsyncMock()
    return plug


# ── 命中 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pulls_the_postman_to_the_only_postal_venue(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    res = await _plugin()._maybe_duty_venue(_ctx(_postman()))
    assert res is not None
    assert res.action == ActionType.VISIT_DISTRICT
    assert res.target_slug == "post_office"
    assert res.target_tile == (46, 100)      # entrance,不是越界的 center


@pytest.mark.anyio
async def test_target_slug_is_resolvable_so_the_move_metric_can_see_it(overlay):
    """memorize 的 move.target 经 resolve_location_id 解析(memorize/basic.py:62-63);
    解析不出就写成 null,生产的到访统计完全看不到这次导流。"""
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    res = await _plugin()._maybe_duty_venue(_ctx(_postman()))
    assert resolve_location_id(res.target_slug, res.target_slug) == "post_office"


@pytest.mark.anyio
async def test_hit_marks_the_plan_interrupted_and_unfollowed(overlay):
    """漏置 plan_followed=False 会让 tick.py:127-131 把自由移动误判成 planned_move。"""
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    plan = HourlyPlan(2, (9, 12), "STUDY", None, "图书馆", 3, "看书")
    ctx = _ctx(_postman(), plan=plan)

    out = await _plugin(skip_decide_when_planned=True).execute(ctx)

    assert out.action_result.action == ActionType.VISIT_DISTRICT
    assert out.action_result.target_slug == "post_office"
    assert out.plan_followed is False
    assert plan.status == "interrupted"


# ── 守卫 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gated_off_is_inert(overlay, monkeypatch):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    monkeypatch.setattr(settings, "duty_venue_enabled", False)
    assert await _plugin()._maybe_duty_venue(_ctx(_postman())) is None


@pytest.mark.anyio
async def test_already_on_site_does_not_pull(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    assert await _plugin()._maybe_duty_venue(_ctx(_postman((46, 103)))) is None


@pytest.mark.anyio
async def test_already_worked_today_does_not_pull(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    r = _postman()
    await get_redis().set(duty_service._duty_work_cooldown_key(r.id), "1")
    assert await _plugin()._maybe_duty_venue(_ctx(r)) is None


@pytest.mark.anyio
async def test_no_duty_venue_declaration_does_not_pull(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    assert await _plugin()._maybe_duty_venue(
        _ctx(_postman(duty_key="tavern_hub"))) is None
    assert await _plugin()._maybe_duty_venue(
        _ctx(_postman(duty_key=None))) is None


@pytest.mark.anyio
async def test_legacy_row_without_declaration_does_not_pull(overlay):
    """存量未回填 → 全镇没有 postal 地点 → 不导流(而不是乱导)。"""
    overlay("post_office", POST_OFFICE)
    assert await _plugin()._maybe_duty_venue(_ctx(_postman())) is None


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["sleeping", "chatting", "socializing"])
async def test_protected_status_is_never_interrupted(overlay, status):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    assert await _plugin()._maybe_duty_venue(
        _ctx(_postman(status=status))) is None


@pytest.mark.anyio
async def test_active_trip_wins(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    ctx = _ctx(_postman())
    ctx.continuation_trip = {"action": "VISIT_DISTRICT"}
    assert await _plugin()._maybe_duty_venue(ctx) is None


@pytest.mark.anyio
async def test_going_home_is_not_a_work_commute(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    plan = HourlyPlan(0, (9, 12), "GO_HOME", None, "home", 3, "回家")
    assert await _plugin()._maybe_duty_venue(
        _ctx(_postman(), plan=plan)) is None


@pytest.mark.anyio
async def test_requires_visit_district_to_be_available(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    ctx = _ctx(_postman())
    ctx.available_actions = [ActionType.IDLE]
    assert await _plugin()._maybe_duty_venue(ctx) is None


# ── 排序不变式 ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_critical_need_still_outranks_the_duty_commute(overlay, monkeypatch):
    """0809「饿死在自家门口」的守卫:临界需求必须排在导流之前。"""
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    monkeypatch.setattr(settings, "realism_enabled", True)
    res = _postman(needs={"energy": 0.05, "satiety": 0.8, "social": 0.8})
    ctx = _ctx(res)

    out = await _plugin(skip_decide_when_planned=True).execute(ctx)

    assert out.action_result.action == ActionType.GO_HOME


@pytest.mark.anyio
async def test_market_cohort_still_outranks_the_duty_commute(overlay, monkeypatch):
    """caravan cohort 是 gameplay 权威,不得被营生导流盖掉。"""
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", True)
    ctx = _ctx(_postman(), [MARKET_DAY])

    with patch.object(crowd_service, "market_day_crowd_cohort",
                      AsyncMock(return_value=frozenset({"post-1"}))):
        out = await _plugin(skip_decide_when_planned=True).execute(ctx)

    assert out.action_result.target_slug == "market_hall"


def test_source_order_is_crowd_then_duty_venue_then_case_two():
    text = DECIDE_SRC.read_text(encoding="utf-8")
    i_crowd = text.index("crowd = await self._maybe_crowd_draw(ctx)")
    i_duty = text.index("duty_venue = await self._maybe_duty_venue(ctx)")
    i_case2 = text.index("# Case 2 (E-09/E-10): plan-priority skip.")
    assert i_crowd < i_duty < i_case2


def test_the_p1_seat_comment_is_replaced_by_the_real_branch():
    text = DECIDE_SRC.read_text(encoding="utf-8")
    assert "_maybe_capability_errand" not in text
    assert "async def _maybe_duty_venue" in text


def test_decide_never_names_the_p1_reverse_lookup_helpers():
    """P1-S9 的 test_market_capability_is_not_used_for_venue_resolution 读本文件全文,
    地点解析必须全部经 duty_service 的包装函数。"""
    text = DECIDE_SRC.read_text(encoding="utf-8")
    assert "capability_locations" not in text
    assert "nearest_capability_location" not in text


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
