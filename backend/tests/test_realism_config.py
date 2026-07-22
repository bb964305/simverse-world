"""REALISM_* config 字段与默认值断言（Task 0）。"""
from app.config import Settings


def test_realism_master_switch_defaults_off():
    s = Settings(_env_file=None)
    assert s.realism_enabled is False  # 默认关，现有行为不变


def test_realism_retrieval_weights_sum_to_one():
    s = Settings(_env_file=None)
    total = (s.realism_retrieval_relevance_weight
             + s.realism_retrieval_recency_weight
             + s.realism_retrieval_importance_weight)
    assert abs(total - 1.0) < 1e-9
    assert s.realism_recency_tau_hours == 72.0


def test_realism_evict_and_mood_defaults():
    s = Settings(_env_file=None)
    assert s.realism_evict_importance_floor == 0.35
    assert s.realism_evict_idle_days == 90
    assert s.realism_proposal_stuck_minutes == 10
    assert s.realism_mood_positive_valence == 0.15
    assert s.realism_mood_negative_valence == -0.2
    assert s.realism_flashbulb_coef == 0.2


def test_realism_env_override(monkeypatch):
    monkeypatch.setenv("REALISM_ENABLED", "true")
    monkeypatch.setenv("REALISM_MOVE_SPEED", "8")
    s = Settings(_env_file=None)
    assert s.realism_enabled is True
    assert s.realism_move_speed == 8
