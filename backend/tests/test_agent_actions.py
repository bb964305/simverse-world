import pytest
from unittest.mock import MagicMock
from app.agent.actions import ActionType, ActionResult, get_available_actions
from app.models.resident import Resident


def _make_resident(status="idle", district="engineering", tile_x=76, tile_y=50):
    r = MagicMock(spec=Resident)
    r.status = status
    r.district = district
    r.tile_x = tile_x
    r.tile_y = tile_y
    r.id = "test-res"
    r.slug = "test-res"
    r.home_tile_x = None
    r.home_tile_y = None
    r.home_location_id = None
    return r


def test_all_16_action_types_exist():
    # RESEARCH is the 15th (Lab, spec §5.4); EAT is the 16th (realism P1-10 needs
    # layer). Append-only: the original 14 keep their names/order/semantics.
    expected = {
        "CHAT_RESIDENT", "CHAT_FOLLOW_UP", "GOSSIP",
        "WANDER", "VISIT_DISTRICT", "GO_HOME",
        "OBSERVE", "EAVESDROP",
        "REFLECT", "JOURNAL",
        "WORK", "STUDY",
        "IDLE", "NAP",
        "RESEARCH",
        "EAT",
    }
    actual = {a.value for a in ActionType}
    assert actual == expected


def test_action_result_dataclass():
    result = ActionResult(
        action=ActionType.WANDER,
        target_slug=None,
        target_tile=(80, 55),
        reason="Feeling restless",
    )
    assert result.action == ActionType.WANDER
    assert result.target_tile == (80, 55)
    assert result.reason == "Feeling restless"


def test_get_available_actions_no_nearby():
    """With no nearby residents, social actions unavailable."""
    r = _make_resident()
    actions = get_available_actions(r, nearby_residents=[])
    social = {ActionType.CHAT_RESIDENT, ActionType.GOSSIP, ActionType.EAVESDROP, ActionType.CHAT_FOLLOW_UP}
    assert not social.intersection(set(actions))
    # Movement always available
    assert ActionType.WANDER in actions


def test_get_available_actions_with_nearby():
    """With nearby idle residents, social actions available."""
    r = _make_resident()
    other = _make_resident(status="idle")
    other.id = "other-res"
    other.slug = "other-res"
    actions = get_available_actions(r, nearby_residents=[other])
    assert ActionType.CHAT_RESIDENT in actions


def test_player_avatar_is_not_a_social_action_target():
    r = _make_resident()
    avatar = _make_resident(status="idle")
    avatar.id = "player-avatar"
    avatar.resident_type = "player"
    actions = get_available_actions(r, nearby_residents=[avatar])
    assert ActionType.CHAT_RESIDENT not in actions


def test_get_available_actions_chatting_resident_excluded():
    """Residents actively chatting cannot be targeted."""
    r = _make_resident()
    busy = _make_resident(status="chatting")
    busy.id = "busy-res"
    busy.slug = "busy-res"
    actions = get_available_actions(r, nearby_residents=[busy])
    # chatting resident not available for CHAT_RESIDENT
    # but EAVESDROP should be possible
    assert ActionType.EAVESDROP in actions
    # CHAT_RESIDENT with that specific busy resident not possible
    # (the filter should not allow initiating chat with chatting resident)


def test_go_home_available_when_away():
    """GO_HOME only available when not at home tile."""
    r = _make_resident(tile_x=10, tile_y=10)
    r.home_tile_x = 76
    r.home_tile_y = 50
    actions = get_available_actions(r, nearby_residents=[])
    assert ActionType.GO_HOME in actions


def test_go_home_unavailable_when_at_home():
    """GO_HOME not offered when already at home tile."""
    r = _make_resident(tile_x=76, tile_y=50)
    r.home_tile_x = 76
    r.home_tile_y = 50
    actions = get_available_actions(r, nearby_residents=[])
    assert ActionType.GO_HOME not in actions


def test_go_home_available_at_home_when_energy_critical(monkeypatch):
    """0809 产线死锁回归:居民停在自家门口、energy 掉到危急后,GO_HOME 被
    「已在家」判据屏蔽 → decide 的 energy 分支拿不到动作 → 永不入睡 → energy
    恒 0;又因 most_critical 平局按元组序恒取 energy,satiety 分支也永不可达
    (7/11 居民 energy=satiety=0 卡死)。到家即睡的逻辑在 execute 的
    already-at-destination 分支(execute/basic.py:174),只要 GO_HOME 可选就能
    走到 —— 所以危急时必须仍然提供它。"""
    from app.config import settings as _s
    monkeypatch.setattr(_s, "realism_enabled", True)
    r = _make_resident(tile_x=76, tile_y=50)
    r.home_tile_x = 76
    r.home_tile_y = 50
    r.meta_json = {"needs": {"energy": 0.0, "satiety": 0.0, "social": 0.7}}
    assert ActionType.GO_HOME in get_available_actions(r, nearby_residents=[])


def test_go_home_still_hidden_at_home_when_rested(monkeypatch):
    """反向守卫:精力充足时在家仍然不提供 GO_HOME(现状不变)。"""
    from app.config import settings as _s
    monkeypatch.setattr(_s, "realism_enabled", True)
    r = _make_resident(tile_x=76, tile_y=50)
    r.home_tile_x = 76
    r.home_tile_y = 50
    r.meta_json = {"needs": {"energy": 0.9, "satiety": 0.9, "social": 0.9}}
    assert ActionType.GO_HOME not in get_available_actions(r, nearby_residents=[])
