"""P3 ③:reload_world 之后新楼必须当场可达(不必等进程重启)。"""
from unittest.mock import AsyncMock

import pytest

from app.agent import pathfinder
from app.config import settings
from app.lab import apply as apply_engine


@pytest.fixture(autouse=True)
def isolated_reload(monkeypatch):
    """reload_world 会去碰全局 engine 与 lore 表;本文件只关心缓存生命周期。"""
    monkeypatch.setattr("app.agent.map_data.load_dynamic_locations",
                        AsyncMock(return_value=0))
    monkeypatch.setattr("app.agent.location_lore.load_dynamic_lore",
                        AsyncMock(return_value=0))
    pathfinder.reset_walkable_cache()
    yield
    pathfinder.reset_walkable_cache()


@pytest.mark.anyio
async def test_gate_off_keeps_the_stale_path_cache(monkeypatch):
    """闸关 = 旧行为:缓存活着,运行中新建的楼要等重启才走得到。"""
    pathfinder.get_reachable_tiles()
    assert pathfinder._walkable_tiles_cache is not None
    await apply_engine.reload_world()
    assert pathfinder._walkable_tiles_cache is not None
    assert pathfinder._reachable_tiles_cache is not None


@pytest.mark.anyio
async def test_gate_on_drops_both_path_caches(monkeypatch):
    monkeypatch.setattr(settings, "world_reload_reset_path_cache", True)
    pathfinder.get_reachable_tiles()
    assert pathfinder._walkable_tiles_cache is not None
    await apply_engine.reload_world()
    assert pathfinder._walkable_tiles_cache is None
    assert pathfinder._reachable_tiles_cache is None


@pytest.mark.anyio
async def test_gate_on_invalidates_the_caravan_route(monkeypatch):
    """build_caravan_route 是 lru_cache(maxsize=1) 且吃 get_reachable_tiles;
    只清 pathfinder 会让商队与居民各持一份世界观。"""
    monkeypatch.setattr(settings, "world_reload_reset_path_cache", True)
    from app.services.caravan_route import build_caravan_route
    build_caravan_route()
    assert build_caravan_route.cache_info().currsize == 1
    await apply_engine.reload_world()
    assert build_caravan_route.cache_info().currsize == 0


@pytest.mark.anyio
async def test_cache_reset_runs_after_the_merge(monkeypatch):
    """_get_forced_walkable 读 LOCATIONS —— 先清后 merge 等于白清。"""
    monkeypatch.setattr(settings, "world_reload_reset_path_cache", True)
    order: list[str] = []

    async def _merge():
        order.append("merge")
        return 0

    def _reset():
        order.append("reset")

    monkeypatch.setattr("app.agent.map_data.load_dynamic_locations", _merge)
    monkeypatch.setattr("app.agent.pathfinder.reset_walkable_cache", _reset)
    await apply_engine.reload_world()
    assert order == ["merge", "reset"]


@pytest.mark.anyio
async def test_caravan_clear_failure_does_not_break_reload(monkeypatch):
    """reload 是 fail-open 链路:路网失效炸了也不许把世界重载带崩。"""
    monkeypatch.setattr(settings, "world_reload_reset_path_cache", True)

    def _boom():
        raise RuntimeError("route cache exploded")

    monkeypatch.setattr(
        "app.services.caravan_route.build_caravan_route.cache_clear", _boom)
    assert await apply_engine.reload_world() == 0
