"""P2-S12: stage_event_cohort —— 照 market_day_crowd_cohort 形状的确定性观众名单。

照抄的四件事(crowd_service.py:122-204 的注释自陈 perf 红线):进程内 TTL 缓存、
asyncio 单飞锁、异常 fail-open、sha256 稳定排序。
刻意不照抄的一件事:**不按站位排除候选**。按站位排除会让到场者每 20s 掉出名单、
后来者被逐批补进,把全镇轮着拉空;不排除则同一场演出内名单稳定,到场者继续占位。
"""
import asyncio

import pytest

from app.agent.actions import ActionType
from app.agent.location_caps import CAP_STAGE
from app.agent.map_data import LOCATIONS
from app.agent import pathfinder
from app.models.resident import Resident
from app.services import crowd_service

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
def _fresh_cohort_cache():
    crowd_service._reset_for_tests()
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


async def _seed(db, n=12, *, status="idle", tile=(75, 56), rtype="npc"):
    made = []
    for i in range(n):
        r = Resident(id=f"aud-{i}", slug=f"aud-{i}", name=f"观众{i}",
                     creator_id="system", resident_type=rtype,
                     district="east_gardens", status=status,
                     tile_x=tile[0], tile_y=tile[1], meta_json={})
        db.add(r)
        made.append(r)
    await db.commit()
    return made


# ── 名单本体 ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_no_stage_event_no_cohort(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session)
    assert await crowd_service.stage_event_cohort(db_session, []) == frozenset()
    assert await crowd_service.stage_event_cohort(db_session, None) == frozenset()


@pytest.mark.anyio
async def test_cohort_is_bounded_and_deterministic(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 12)

    first = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT], ttl=0)
    second = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT], ttl=0)

    assert first == second
    assert len(first) == crowd_service.STAGE_EVENT_CROWD_LIMIT == 6
    assert first <= {f"aud-{i}" for i in range(12)}


@pytest.mark.anyio
async def test_a_smaller_town_yields_a_smaller_cohort(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 3)
    assert len(await crowd_service.stage_event_cohort(
        db_session, [STAGE_EVENT], ttl=0)) == 3


@pytest.mark.anyio
async def test_a_different_event_reshuffles_the_cohort(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 12)
    other = {**STAGE_EVENT, "id": "stage-2"}

    a = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT], ttl=0)
    b = await crowd_service.stage_event_cohort(db_session, [other], ttl=0)
    assert a != b


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["sleeping", "chatting", "socializing"])
async def test_protected_status_is_never_invited(db_session, overlay, status):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 6, status=status)
    assert await crowd_service.stage_event_cohort(
        db_session, [STAGE_EVENT], ttl=0) == frozenset()


@pytest.mark.anyio
async def test_non_sim_residents_are_never_invited(db_session, overlay):
    """UGC character / 玩家角色不是自治居民,不参与人流。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 6, rtype="character")
    assert await crowd_service.stage_event_cohort(
        db_session, [STAGE_EVENT], ttl=0) == frozenset()


@pytest.mark.anyio
async def test_residents_already_at_the_venue_keep_their_seat(db_session, overlay):
    """刻意不按站位排除:否则到场者每 20s 掉出名单、后来者被逐批补进,整镇被轮着拉空。
    名单在同一场演出内必须稳定。"""
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 12)
    before = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT], ttl=0)

    for rid in before:                       # 所有人都已到场
        r = await db_session.get(Resident, rid)
        r.tile_x, r.tile_y = 174, 45
    await db_session.commit()

    after = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT], ttl=0)
    assert after == before


@pytest.mark.anyio
async def test_a_venue_without_the_declaration_yields_no_cohort(db_session, overlay):
    overlay("theater", THEATER)
    await _seed(db_session, 12)
    assert await crowd_service.stage_event_cohort(
        db_session, [STAGE_EVENT], ttl=0) == frozenset()


# ── perf 红线:缓存 + 单飞 ────────────────────────────────────────────

@pytest.mark.anyio
async def test_the_cohort_is_cached_within_the_ttl(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 12)
    first = await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT])

    for i in range(12, 18):                  # 缓存期内新增居民不得改变名单
        db_session.add(Resident(id=f"aud-{i}", slug=f"aud-{i}", name=f"观众{i}",
                                creator_id="system", resident_type="npc",
                                district="east_gardens", status="idle",
                                tile_x=75, tile_y=56, meta_json={}))
    await db_session.commit()

    assert await crowd_service.stage_event_cohort(db_session, [STAGE_EVENT]) == first


@pytest.mark.anyio
async def test_concurrent_ticks_share_a_single_query(db_session, overlay):
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    await _seed(db_session, 12)
    calls = {"n": 0}
    real_execute = db_session.execute

    async def counting_execute(*args, **kwargs):
        calls["n"] += 1
        return await real_execute(*args, **kwargs)

    db_session.execute = counting_execute
    try:
        results = await asyncio.gather(*[
            crowd_service.stage_event_cohort(db_session, [STAGE_EVENT])
            for _ in range(8)
        ])
    finally:
        db_session.execute = real_execute

    assert len(set(results)) == 1
    assert calls["n"] == 1, "单飞锁没生效 —— 每 tick 一次居民表查询是 perf 红线"


@pytest.mark.anyio
async def test_a_database_failure_fails_open_to_an_empty_cohort(overlay):
    """查询炸了就不拉人,而不是把异常抛进 decide 相位(tick.py:102-104 会 break 整
    条相位链)。"""
    from unittest.mock import AsyncMock
    overlay("theater", THEATER, capabilities={CAP_STAGE: {}})
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("db down")
    assert await crowd_service.stage_event_cohort(db, [STAGE_EVENT]) == frozenset()


def test_stage_and_market_cohorts_do_not_share_a_lock_or_a_cache():
    """共用单飞锁会让两条人流互相排队;共用缓存会让键互相驱逐。"""
    assert crowd_service._stage_cohort_lock is not crowd_service._market_cohort_lock
    assert crowd_service._stage_cohort_cache is not crowd_service._market_cohort_cache


def test_reset_for_tests_clears_both_cohort_caches():
    crowd_service._stage_cohort_cache[("x", "", "", "theater")] = (0.0, frozenset())
    crowd_service._market_cohort_cache[("x", "", "", False)] = (0.0, frozenset())
    crowd_service._reset_for_tests()
    assert not crowd_service._stage_cohort_cache
    assert not crowd_service._market_cohort_cache


def test_action_type_enum_is_untouched():
    actions = list(ActionType)
    assert len(actions) == 16
    assert actions[14] == ActionType.RESEARCH and actions[15] == ActionType.EAT
