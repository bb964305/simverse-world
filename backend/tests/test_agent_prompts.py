"""Task 2（burn-in 修复批次 1）：社交半径配置断言 + decide 社交软提示。"""
from unittest.mock import MagicMock

import pytest
import yaml

from app.agent.registry import CONFIG_DIR


def _perceive_radius(config_name: str) -> int:
    with open(CONFIG_DIR / f"{config_name}.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["phases"]["perceive"]["params"]["radius"]


def test_social_radius_expanded():
    assert _perceive_radius("default") == 18
    assert _perceive_radius("extravert") == 24
    assert _perceive_radius("introvert") == 10


def _mk_resident():
    r = MagicMock()
    r.name = "克劳斯"
    r.persona_md = "p"
    r.tile_x = 1
    r.tile_y = 1
    r.status = "idle"
    r.meta_json = None
    r.mood_json = None
    return r


def _mk_nearby():
    nearby = MagicMock()
    nearby.name = "梅"
    nearby.slug = "mei"
    nearby.status = "idle"
    nearby.tile_x = 2
    nearby.tile_y = 1
    return nearby


def test_decision_prompt_social_hint_when_nearby():
    from app.agent.actions import ActionType
    from app.agent.prompts import build_decision_prompt

    system, user = build_decision_prompt(
        resident=_mk_resident(), schedule_phase="afternoon", world_time="14:00",
        nearby_residents=[_mk_nearby()], memories=[], today_actions=[],
        available_actions=[ActionType.CHAT_RESIDENT, ActionType.IDLE],
        max_daily_actions=20,
    )
    assert "主动搭话" in system + user


def test_decision_prompt_no_social_hint_when_alone():
    from app.agent.actions import ActionType
    from app.agent.prompts import build_decision_prompt

    system, user = build_decision_prompt(
        resident=_mk_resident(), schedule_phase="afternoon", world_time="14:00",
        nearby_residents=[], memories=[], today_actions=[],
        available_actions=[ActionType.IDLE], max_daily_actions=20,
    )
    assert "主动搭话" not in system + user


@pytest.mark.parametrize(
    ("realism_enabled", "target_slug_line", "target_tile_line", "movement_rule"),
    [
        (
            False,
            '"target_slug": "<居民slug或null>"',
            '"target_tile": [x, y] 或 null',
            "WANDER/VISIT_DISTRICT 填入 target_tile（使用地点入口坐标）",
        ),
        (
            True,
            '"target_slug": "<居民slug、地点ID/名称或null>"',
            '"target_tile": null',
            "VISIT_DISTRICT/WANDER 可在 target_slug 填入地点ID",
        ),
    ],
)
def test_decision_prompt_movement_contract_matches_realism_gate(
    monkeypatch,
    realism_enabled,
    target_slug_line,
    target_tile_line,
    movement_rule,
):
    from app.agent.actions import ActionType
    from app.agent.prompts import build_decision_prompt
    from app.config import settings

    monkeypatch.setattr(settings, "realism_enabled", realism_enabled)
    system, _ = build_decision_prompt(
        resident=_mk_resident(),
        schedule_phase="afternoon",
        world_time="14:00",
        nearby_residents=[],
        memories=[],
        today_actions=[],
        available_actions=[ActionType.WANDER, ActionType.VISIT_DISTRICT],
        max_daily_actions=20,
    )

    assert target_slug_line in system
    assert target_tile_line in system
    assert movement_rule in system
