"""收口（ROADMAP #5）：F2 的 12 个 CIVIC_ 旋钮注册进 Settings。

历史：F2 合入时这些旋钮只在调用时读 os.environ（.env.example 注释着
「不经 Settings」），一致性由 RUNTIME_ENV_KEYS 白名单放行——那是收口前的
临时形态。civic_membership._settings_default 从第一天起就预留了挂钩：
「收口把同名字段加进 Settings 后自动生效」。本文件钉住收口后的三层优先级：

    进程 env（调用时读，monkeypatch 友好） > Settings（含 .env 文件） > 代码默认

把字段加进 Settings 不打断既有的 monkeypatch.setenv 测试——reader 永远先看
os.environ，Settings 只接管「env 未设」时的兜底默认。
"""
import pytest

from app.config import Settings, settings
from app.services import civic_membership as cm

#: (Settings 字段名, 调用时 reader, 保守默认值)——默认值必须与
#: civic_membership 的代码默认逐字一致，否则「加进 Settings」本身就成了
#: 一次静默的行为变更。
KNOBS = [
    ("civic_promotion_mode", cm.promotion_mode, "off"),
    ("civic_auto_demotion_enabled", cm.auto_demotion_enabled, False),
    ("civic_promotion_min_world_days", cm.min_world_days, 30.0),
    ("civic_promotion_min_peers", cm.min_peers, 3),
    ("civic_promotion_min_familiarity", cm.min_familiarity, 0.20),
    ("civic_peer_seasoning_world_days", cm.peer_seasoning_world_days, 28.0),
    ("civic_promotion_max_per_run", cm.promotion_max_per_run, 5),
    ("civic_promotion_breaker_fraction", cm.promotion_breaker_fraction, 0.20),
    ("civic_promotion_breaker_min_abs", cm.promotion_breaker_min_abs, 3),
    ("civic_min_electorate", cm.min_electorate, 3),
    ("civic_min_tenure_world_days", cm.min_tenure_world_days, 12.0),
    ("civic_promotion_cooldown_world_days", cm.promotion_cooldown_world_days, 12.0),
]


def test_all_civic_knobs_are_settings_fields_with_conservative_defaults():
    fields = Settings.model_fields
    for name, _reader, default in KNOBS:
        assert name in fields, f"收口缺口：{name} 不在 Settings 里"
        assert fields[name].default == default, (
            f"{name} 的 Settings 默认值必须与 civic_membership 的代码默认一致"
            f"（关/保守），期望 {default!r}，实得 {fields[name].default!r}")


def test_promotion_mode_default_stays_off():
    """三态闸门默认必须是 off——本批不开闸（行为开闸与代码变更分开）。"""
    assert Settings.model_fields["civic_promotion_mode"].default == "off"


def test_settings_value_feeds_reader_when_env_unset(monkeypatch):
    """env 未设时 reader 读 Settings——_settings_default 挂钩真的通了。"""
    monkeypatch.delenv("CIVIC_PROMOTION_MAX_PER_RUN", raising=False)
    monkeypatch.setattr(settings, "civic_promotion_max_per_run", 7)
    assert cm.promotion_max_per_run() == 7


def test_env_still_wins_over_settings(monkeypatch):
    """进程 env 永远压过 Settings——F2 近百条 monkeypatch.setenv 测试赖此成立。"""
    monkeypatch.setattr(settings, "civic_promotion_max_per_run", 7)
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "2")
    assert cm.promotion_max_per_run() == 2


def test_mode_env_wins_and_settings_fallback(monkeypatch):
    """三态闸门同样走这条优先级链。"""
    monkeypatch.setattr(settings, "civic_promotion_mode", "shadow")
    monkeypatch.delenv("CIVIC_PROMOTION_MODE", raising=False)
    assert cm.promotion_mode() == "shadow"
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "off")
    assert cm.promotion_mode() == "off"
