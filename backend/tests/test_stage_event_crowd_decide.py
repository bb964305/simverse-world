"""P2-S13: decide 的 _maybe_stage_crowd 分支 + §③ 路 B 的不变式守卫。

四类断言:
  1 闸与守卫(闸/可用集/status/粘性行程/GO_HOME/不在名单/已在场/无演出);
  2 命中后的上下文副作用(plan_followed=False + plan.status=interrupted),漏置会让
    tick.py:127-131 把这次自由移动误判成 planned_move 写进粘性行程;
  3 排序不变式:临界需求 > caravan cohort > 营生导流 > 看戏 > 计划跳过,源码顺序
    由文本断言钉死;
  4 **路 B 的核心论证**:actions.py 一个字不改,所以其它地点的 CHAT_RESIDENT 行为
    逐条不变;鸡生蛋只被「人真的到场」这一个外力打破。
"""
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.actions import ActionType, get_available_actions
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS
from app.agent import pathfinder
from app.agent.plan_target import resolve_location_id
from app.agent.schemas import HourlyPlan, TickContext
from app.config import Settings, settings
from app.services import crowd_service

BACKEND = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = BACKEND / ".env.example"
DEPLOY_ENV_EXAMPLE = BACKEND.parent / "deploy" / "backend" / ".env.example"
DECIDE_SRC = BACKEND / "app" / "agent" / "phases" / "decide" / "basic.py"
ACTIONS_SRC = BACKEND / "app" / "agent" / "actions.py"

THEATER = {
    "name": "剧院", "type": "public", "role": "culture",
    "bounds": (172, 40, 178, 50), "center": (175, 45), "entrance": (172, 45),
    "description": "小镇剧院:辩论、公开课、戏与人群",
    "boosted_actions": ["OBSERVE", "CHAT_RESIDENT"],
}
STAGE_EVENT = {
    "id": "stage-1", "type": "script", "title": "一场辩论",
    "starts_at": "2026-08-17T10:00:00+00:00",
    "ends_at": "2026-08-17T11:00:00+00:00",
    "payload_json": {"location_id": "theater", "debate_id": "d1"},
}


@pytest.fixture(autouse=True)
def _quiet_world(monkeypatch):
    """默认关掉会抢在本分支之前的三条通路,单测只留一个变量。"""
    crowd_service._reset_for_tests()
    monkeypatch.setattr(settings, "stage_event_crowd_enabled", True)
    monkeypatch.setattr(settings, "realism_crowd_enabled", False)
    monkeypatch.setattr(settings, "caravan_lifecycle_enabled", False)
    monkeypatch.setattr(settings, "duty_venue_enabled", False)
    yield
    crowd_service._reset_for_tests()


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
        pathfinder.reset_walkable_cache()
        return slug

    yield _merge
    for slug in added:
        LOCATIONS.pop(slug, None)
    pathfinder.reset_walkable_cache()


def _res(rid="aud-0", tile=(75, 56), *, status="idle", rtype="npc"):
    return SimpleNamespace(
        id=rid, slug=rid, name="观众", resident_type=rtype, status=status,
        tile_x=tile[0], tile_y=tile[1], meta_json={},
        home_location_id=None, home_tile_x=5, home_tile_y=5,
    )


def _ctx(resident, world_events=None, plan=None):
    ctx = TickContext(db=AsyncMock(), resident=resident, world_time="20:00",
                      hour=20, schedule_phase="傍晚",
                      current_plan=plan, scheduled_plan=plan)
    ctx.world_events = world_events if world_events is not None else [STAGE_EVENT]
    ctx.available_actions = [ActionType.VISIT_DISTRICT, ActionType.OBSERVE,
                             ActionType.IDLE]
    return ctx


def _plugin(**params):
    from app.agent.phases.decide.basic import BasicDecidePlugin
    plug = BasicDecidePlugin(params=params or None)
    plug._load_memories = AsyncMock()
    return plug


def _cohort(*ids):
    return patch.object(crowd_service, "stage_event_cohort",
                        AsyncMock(return_value=frozenset(ids)))


# ── 闸本身 ────────────────────────────────────────────────────────────

def test_flag_defaults_to_off():
    assert Settings.model_fields["stage_event_crowd_enabled"].default is False


def test_flag_is_documented_as_false_in_both_env_templates():
    """STAGE_EVENT_CROWD_ENABLED 不在 GOVERNANCE_PREFIXES(CIVIC_/REP_/POLIS_OFFICE_)
    里,现成的那条 parity 扫不到它 —— 而「扫不到」的表现和「模板里没有这个键」一模
    一样。这里按键名兜底,不依赖任何前缀。"""
    for path in (ENV_EXAMPLE, DEPLOY_ENV_EXAMPLE):
        assert "STAGE_EVENT_CROWD_ENABLED=false" in path.read_text(encoding="utf-8"), path


@pytest.mark.anyio
async def test_gated_off_is_inert(overlay, monkeypatch):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_crowd_enabled", False)
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(_ctx(_res())) is None


# ── 命中 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_pulls_a_listed_resident_to_the_venue(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    with _cohort("aud-0", "aud-1"):
        res = await _plugin()._maybe_stage_crowd(_ctx(_res()))
    assert res is not None
    assert res.action == ActionType.VISIT_DISTRICT
    assert res.target_slug == "theater"
    assert res.target_tile == (172, 45)      # entrance,不是孤岛 center(175,45)


@pytest.mark.anyio
async def test_target_slug_is_resolvable_so_the_visit_metric_can_see_it(overlay):
    """memorize 的 move.target 经 resolve_location_id 解析(memorize/basic.py:62-63);
    解析不出就写成 null,M3/M4 的到访统计完全看不到这次导流。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    with _cohort("aud-0"):
        res = await _plugin()._maybe_stage_crowd(_ctx(_res()))
    assert resolve_location_id(res.target_slug, res.target_slug) == "theater"


@pytest.mark.anyio
async def test_hit_marks_the_plan_interrupted_and_unfollowed(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    plan = HourlyPlan(2, (18, 22), "STUDY", None, "图书馆", 3, "看书")
    ctx = _ctx(_res(), plan=plan)
    with _cohort("aud-0"):
        out = await _plugin(skip_decide_when_planned=True).execute(ctx)
    assert out.action_result.target_slug == "theater"
    assert out.plan_followed is False
    assert plan.status == "interrupted"


@pytest.mark.anyio
async def test_the_branch_never_claims_a_market_trip(overlay):
    """market_trip_event_id 是集市专用:tick.py:155-162 会把行程的 kind/location 写死
    成 market_day/market_hall,借用它等于把观众登记成买家。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    ctx = _ctx(_res())
    with _cohort("aud-0"):
        await _plugin()._maybe_stage_crowd(ctx)
    assert ctx.market_trip_event_id is None


# ── 守卫 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_not_on_the_list_is_not_pulled(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    with _cohort("someone-else"):
        assert await _plugin()._maybe_stage_crowd(_ctx(_res())) is None


@pytest.mark.anyio
async def test_already_at_the_venue_is_not_pulled_again(overlay):
    """站位判断必须穿透 outdoor 遮蔽(get_location_id_at(174,45)=="east_gardens"),
    否则人站在剧院里还会被每 tick 再拉一次。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(
            _ctx(_res(tile=(174, 45)))) is None


@pytest.mark.anyio
async def test_no_stage_event_does_not_query_the_cohort_at_all(overlay):
    """没戏可看时连查询都不该发生 —— 解析在前、查库在后。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    probe = AsyncMock(return_value=frozenset({"aud-0"}))
    with patch.object(crowd_service, "stage_event_cohort", probe):
        assert await _plugin()._maybe_stage_crowd(_ctx(_res(), [])) is None
    probe.assert_not_awaited()


@pytest.mark.anyio
async def test_legacy_row_without_declaration_does_not_pull(overlay):
    overlay("theater", THEATER)
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(_ctx(_res())) is None


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["sleeping", "chatting", "socializing"])
async def test_protected_status_is_never_interrupted(overlay, status):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(
            _ctx(_res(status=status))) is None


@pytest.mark.anyio
async def test_active_trip_wins(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    ctx = _ctx(_res())
    ctx.continuation_trip = {"action": "VISIT_DISTRICT"}
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(ctx) is None


@pytest.mark.anyio
async def test_going_home_is_not_entertainment(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    plan = HourlyPlan(0, (18, 22), "GO_HOME", None, "home", 3, "回家")
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(
            _ctx(_res(), plan=plan)) is None


@pytest.mark.anyio
async def test_requires_visit_district_to_be_available(overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    ctx = _ctx(_res())
    ctx.available_actions = [ActionType.IDLE]
    with _cohort("aud-0"):
        assert await _plugin()._maybe_stage_crowd(ctx) is None


@pytest.mark.anyio
async def test_critical_need_still_outranks_the_show(overlay, monkeypatch):
    """0809「饿死在自家门口」的守卫:临界需求必须排在人流拉力之前。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "realism_enabled", True)
    res = _res()
    res.meta_json = {"needs": {"energy": 0.05, "satiety": 0.8, "social": 0.8}}
    ctx = _ctx(res)
    ctx.available_actions.append(ActionType.GO_HOME)
    with _cohort("aud-0"):
        out = await _plugin(skip_decide_when_planned=True).execute(ctx)
    assert out.action_result.action == ActionType.GO_HOME


# ── 排序:源码顺序 ────────────────────────────────────────────────────

def test_source_order_is_crowd_then_duty_then_stage_then_case_two():
    text = DECIDE_SRC.read_text(encoding="utf-8")
    i_crowd = text.index("crowd = await self._maybe_crowd_draw(ctx)")
    i_duty = text.index("duty_venue = await self._maybe_duty_venue(ctx)")
    i_stage = text.index("stage_crowd = await self._maybe_stage_crowd(ctx)")
    i_case2 = text.index("# Case 2 (E-09/E-10): plan-priority skip.")
    assert i_crowd < i_duty < i_stage < i_case2


def test_decide_never_names_the_p1_reverse_lookup_helpers():
    """P1-S9 的 test_market_capability_is_not_used_for_venue_resolution 读本文件;
    地点解析必须全部经 crowd_service 的包装函数。

    与计划原文的偏差(已回源码核实):计划写「``capability_location_at`` not in
    全文」,但 P1-S6/S7 早已在 ``_maybe_needs_action`` 的餐馆判据里用它(现源码
    ``decide/basic.py`` 有且仅有那 2 处),该断言对今天的源码恒假、与本 step 无关。
    真正的 P1-S9 守卫(tests/test_market_hall_constant.py:45-62)只禁
    ``capability_locations`` / ``nearest_capability_location``,且只看非注释行 ——
    这里按它的真实口径钉住全文,再单独钉住本 step 新增的那个方法自身一个反查
    helper 都不许提,语义与计划一致而不误伤既有的餐馆路径。
    """
    from app.agent.phases.decide.basic import BasicDecidePlugin
    text = DECIDE_SRC.read_text(encoding="utf-8")
    assert "capability_locations" not in text
    assert "nearest_capability_location" not in text
    branch = inspect.getsource(BasicDecidePlugin._maybe_stage_crowd)
    for name in ("capability_locations", "nearest_capability_location",
                 "capability_location_at"):
        assert name not in branch, name


def test_decide_adds_no_bare_theater_or_market_hall_literal():
    """场地来自事件 + 能力声明,不是 decide 里的字面量(P1-S9 另有一条同款守卫)。"""
    import re
    for i, line in enumerate(DECIDE_SRC.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        assert not re.search(r"[\"'](theater|market_hall)[\"']", line), f"{i}: {line}"


# ── §③ 路 B 的核心论证:actions.py 零改动 ─────────────────────────────

def test_the_chat_gate_source_is_untouched_verbatim():
    """路 B 的全部要点:CHAT_RESIDENT 的判据一个字都不改。"""
    text = ACTIONS_SRC.read_text(encoding="utf-8")
    assert ('    idle_nearby = [r for r in nearby_residents\n'
            '                   if _targetable(r) and r.status in ("idle", "walking")]'
            ) in text
    assert ('    if idle_nearby:\n'
            '        available.extend(_SOCIAL_NEEDS_IDLE_TARGET)') in text
    for token in ("stage", "cohort", "crowd_service", "world_events"):
        assert token not in text, token


@pytest.mark.parametrize("gate", [True, False])
@pytest.mark.parametrize("tile", [(75, 56), (174, 45)])
def test_other_locations_keep_their_exact_authorization_set(
        overlay, monkeypatch, gate, tile):
    """授权集是 (居民, 附近的人) 的纯函数 —— 演出、闸态、站位都不改变它。

    这是「不破坏其它地点的 CHAT_RESIDENT 行为」的机器证明:get_available_actions
    根本不接收 world_events,本段也没有给它加任何参数或分支。
    """
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    monkeypatch.setattr(settings, "stage_event_crowd_enabled", gate)
    me = _res("me", tile)
    lonely = get_available_actions(me, nearby_residents=[])
    assert ActionType.CHAT_RESIDENT not in lonely
    assert ActionType.GOSSIP not in lonely and ActionType.EAVESDROP not in lonely

    peer = _res("peer", tile)
    with_peer = get_available_actions(me, nearby_residents=[peer])
    assert ActionType.CHAT_RESIDENT in with_peer


def test_the_lock_opens_by_itself_once_the_cohort_arrives(overlay):
    """§③ 的结论:名单把人送到场后,idle_nearby 自然非空,CHAT_RESIDENT 自动解锁 ——
    鸡生蛋被外力打破一次即可自持,不需要碰 actions.py 一个字。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    me = _res("me", (174, 45))
    assert ActionType.CHAT_RESIDENT not in get_available_actions(me, [])  # 到场前:空场
    arrived = [_res(f"aud-{i}", (174, 45)) for i in range(3)]
    assert ActionType.CHAT_RESIDENT in get_available_actions(me, arrived)


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
