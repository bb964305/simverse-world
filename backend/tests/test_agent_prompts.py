"""Task 2（burn-in 修复批次 1）：社交半径配置断言 + decide 社交软提示。"""
from unittest.mock import MagicMock

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
