"""P1-S3: LOCATION_CAPABILITIES_ENABLED 闸 + location_category 的能力派生层。

闸关 = location_category 逐字节旧行为(显式 category 键 → _DINING_LOCATIONS 白名单)。
闸开 = 中间多插一级「从声明派生」。白名单不删:它是最后一级 fallback,删掉的话一旦
某处声明没落地就静默失去 dining。
"""
from pathlib import Path

import pytest

from app.agent.location_caps import CAP_DINING
from app.agent.map_data import LOCATIONS, _DINING_LOCATIONS, location_category
from app.config import Settings, settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


@pytest.fixture
def temp_location():
    added: list[str] = []

    def _add(slug: str, extra: dict) -> str:
        assert slug not in LOCATIONS, slug
        data = {"name": "临时", "type": "public",
                "bounds": (0, 0, 1, 1), "center": (0, 0)}
        data.update(extra)
        LOCATIONS[slug] = data
        added.append(slug)
        return slug

    yield _add
    for slug in added:
        LOCATIONS.pop(slug, None)


def test_flag_defaults_to_off():
    assert Settings.model_fields["location_capabilities_enabled"].default is False


def test_flag_is_documented_as_false_in_backend_env_example():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "LOCATION_CAPABILITIES_ENABLED=false" in text


def test_dining_allowlist_is_not_deleted():
    assert _DINING_LOCATIONS == {"cafe", "tavern"}


@pytest.mark.parametrize("slug", sorted(LOCATIONS))
def test_category_is_identical_with_the_flag_on_and_off(slug, monkeypatch):
    monkeypatch.setattr(settings, "location_capabilities_enabled", False)
    legacy = location_category(slug)
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    assert location_category(slug) == legacy, slug


def test_cafe_and_tavern_stay_dining_under_both_flag_states(monkeypatch):
    for state in (False, True):
        monkeypatch.setattr(settings, "location_capabilities_enabled", state)
        assert location_category("cafe") == "dining"
        assert location_category("tavern") == "dining"


def test_declaration_derives_a_category_only_when_the_flag_is_on(
        temp_location, monkeypatch):
    temp_location("t_canteen", {"capabilities": {CAP_DINING: {}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", False)
    assert location_category("t_canteen") is None
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    assert location_category("t_canteen") == "dining"


def test_explicit_category_key_still_wins_over_the_declaration(
        temp_location, monkeypatch):
    temp_location("t_odd", {"category": "lodging",
                           "capabilities": {CAP_DINING: {}}})
    for state in (False, True):
        monkeypatch.setattr(settings, "location_capabilities_enabled", state)
        assert location_category("t_odd") == "lodging"


def test_capability_without_a_category_derives_nothing(
        temp_location, monkeypatch):
    temp_location("t_shed", {"capabilities": {"research": {}, "market": {}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    assert location_category("t_shed") is None


def test_nearest_dining_location_picks_up_a_declared_diner_when_the_flag_is_on(
        temp_location, monkeypatch):
    """「顺着 category 长」的红利:nearest_dining_location 认新声明。

    坐标必须落在可达域内(实测 (60,60) 与 town hub 连通):P1-S8 起闸开的
    nearest_dining_location 委托 nearest_capability_location,后者会过滤掉不在
    pathfinder.get_reachable_tiles() 里的入口 —— 原来的 (1,1) 在 walkable 域
    (x≥14)之外,只靠 forced-walkable 自证成功,是个孤岛目标。
    """
    from app.agent.map_data import nearest_dining_location
    temp_location("t_near", {"bounds": (58, 58, 62, 62), "center": (60, 60),
                            "entrance": (60, 60),
                            "capabilities": {CAP_DINING: {}}})
    monkeypatch.setattr(settings, "location_capabilities_enabled", False)
    assert nearest_dining_location((60, 60)) in _DINING_LOCATIONS
    monkeypatch.setattr(settings, "location_capabilities_enabled", True)
    assert nearest_dining_location((60, 60)) == "t_near"
