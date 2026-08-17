"""P3 开闸硬顺序必须写在 .env.example 里(先例:79ef1ce TOWN_DUTY_FUNDING)。

七道新闸散在 config.py 各处;运维只读 .env.example。漏一条就会出现
「开了闸却像没生效」这种假报失败。

注意本文件的 TDD 红点只有 ``test_hard_ordering_is_spelled_out`` 两条 ——
七个键的 presence/默认值那 14 条参数化在本 step 起点即绿:它们由 P3-S5..S11
各自同 commit 写入两份模板,这里把它们钉成回归守卫(将来谁删了赋值行当场红),
不是假 TDD。
"""
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
BACKEND_ENV = ROOT / ".env.example"
DEPLOY_ENV = ROOT.parent / "deploy" / "backend" / ".env.example"

P3_FLAGS = (
    "CIVIC_BUILD_SCHEMA_ENABLED",
    "CIVIC_BUILD_VALIDATE_ENABLED",
    "WORLD_RELOAD_RESET_PATH_CACHE",
    "LOCATION_SPECIFIC_FIRST_ENABLED",
    "CIVIC_EFFECT_AUDIT_ENABLED",
    "CIVIC_BUILD_OPENING_EVENT_ENABLED",
    "CIVIC_FACTS_PLACES_DYNAMIC_RESERVE",
)


@pytest.mark.parametrize("env_file", [BACKEND_ENV, DEPLOY_ENV])
@pytest.mark.parametrize("flag", P3_FLAGS)
def test_flag_is_documented_and_off(env_file, flag):
    text = env_file.read_text(encoding="utf-8")
    assert f"{flag}=" in text, f"{env_file.name} 缺 {flag}"
    line = next(ln for ln in text.splitlines()
                if ln.startswith(f"{flag}="))
    assert line.split("=", 1)[1].strip() in {"false", "0"}, \
        f"{env_file.name} 的 {flag} 不是默认关:{line}"


def test_defaults_match_the_settings_class():
    s = Settings()
    assert s.civic_build_schema_enabled is False
    assert s.civic_build_validate_enabled is False
    assert s.world_reload_reset_path_cache is False
    assert s.location_specific_first_enabled is False
    assert s.civic_effect_audit_enabled is False
    assert s.civic_build_opening_event_enabled is False
    assert s.civic_facts_places_dynamic_reserve == 0


@pytest.mark.parametrize("env_file", [BACKEND_ENV, DEPLOY_ENV])
def test_hard_ordering_is_spelled_out(env_file):
    text = env_file.read_text(encoding="utf-8")
    assert "P3 开闸硬顺序" in text
    assert "REALISM_CROWD_ENABLED" in text, \
        "落成庆典没有这道闸就零位移拉力,必须点名"
    assert "LOCATION_SPECIFIC_FIRST_ENABLED" in text
