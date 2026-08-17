"""P2-S2: 营生 → 现场能力的映射与四个纯查询(零生产调用方,不挂闸)。

核心是一条对 design §①-A 伪代码的校正:现场解析必须走 capability_location_at,
不能走 get_location_id_at —— 后者首命中即返,而 post_office(44,100,48,106) 完全落在
outdoor 街区 south_quarter(42,100,135,109) 内部。test_masking_is_real_and_the_venue
_lookup_sees_through_it 同时钉死「遮蔽是真的」与「穿透查得到」两件事。
"""
import re
from pathlib import Path

import pytest

from app.agent.location_caps import (
    CAPABILITIES, CAP_POSTAL, CIVIC_GRANTABLE_CAPABILITIES,
)
from app.agent.map_data import LOCATIONS, get_location_id_at
from app.services import duty_service

DUTY_SERVICE_SRC = (Path(__file__).resolve().parents[1]
                    / "app" / "services" / "duty_service.py")

# 生产 dynamic_locations 里 post_office 那行的 data_json(2026-08 公投建,active=t),
# capabilities 由调用方按场景决定加不加 —— 存量行今天**没有**这个键。
POST_OFFICE = {
    "name": "邮局", "type": "public", "role": "logistics",
    "bounds": (44, 100, 48, 106), "center": (46, 103), "entrance": (46, 100),
    "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
    "boosted_actions": ["WORK"],
}


@pytest.fixture
def overlay():
    """模拟 load_dynamic_locations 的合入:追加到 LOCATIONS 尾部,再还原。"""
    added: list[str] = []

    def _merge(slug: str, data: dict, capabilities=None) -> str:
        assert slug not in LOCATIONS, slug
        row = dict(data)
        if capabilities is not None:
            row["capabilities"] = capabilities
        LOCATIONS[slug] = row
        added.append(slug)
        return slug

    yield _merge
    for slug in added:
        LOCATIONS.pop(slug, None)


def _postman(tile=(0, 0), *, duty_key="postman", resident_type="npc"):
    from types import SimpleNamespace
    meta = {"duty": {"key": duty_key}} if duty_key else {}
    return SimpleNamespace(
        id="post-1", slug="luo-xiaozhou", name="骆小舟",
        resident_type=resident_type, status="idle",
        tile_x=tile[0], tile_y=tile[1], meta_json=meta,
    )


# ── 映射表本身 ─────────────────────────────────────────────────────────

def test_the_mapping_is_exactly_one_entry_for_now():
    """讲师的 stage/academy 归 design_P2.md 的 #8,不在本段。"""
    assert duty_service.DUTY_VENUE_CAPABILITY == {"postman": CAP_POSTAL}


def test_mapped_capabilities_are_registered_and_civic_grantable():
    for cap in duty_service.DUTY_VENUE_CAPABILITY.values():
        assert cap in CAPABILITIES, cap
        assert cap in CIVIC_GRANTABLE_CAPABILITIES, cap


def test_mapped_duty_keys_all_have_a_work_handler():
    """没有 WORK 产出的营生谈不上「现场」。"""
    assert set(duty_service.DUTY_VENUE_CAPABILITY) <= set(duty_service._WORK_HANDLERS)


# ── duty_venue_capability ─────────────────────────────────────────────

def test_capability_is_read_for_the_postman_only():
    assert duty_service.duty_venue_capability(_postman()) == CAP_POSTAL
    assert duty_service.duty_venue_capability(_postman(duty_key="tavern_hub")) is None
    assert duty_service.duty_venue_capability(_postman(duty_key=None)) is None


def test_untrusted_provenance_cannot_self_declare_a_duty_venue():
    """UGC 居民往 meta_json 里塞 duty 无效(resident_privilege_policy.py:105-110)。"""
    assert duty_service.duty_venue_capability(
        _postman(resident_type="character")) is None


# ── duty_venue_location_at / nearest_duty_venue ───────────────────────

def test_masking_is_real_and_the_venue_lookup_sees_through_it(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    # 遮蔽是真的:首命中返回的是 outdoor 街区,不是邮局。
    assert get_location_id_at(46, 103) == "south_quarter"
    assert get_location_id_at(46, 100) == "south_quarter"
    # 能力反查穿透遮蔽。
    assert duty_service.duty_venue_location_at(_postman((46, 103))) == "post_office"
    assert duty_service.duty_venue_location_at(_postman((46, 100))) == "post_office"


def test_outside_the_venue_returns_none(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    assert duty_service.duty_venue_location_at(_postman((75, 56))) is None


def test_legacy_row_without_the_declaration_is_inert(overlay):
    """存量 dynamic_locations 行没有 capabilities 键 —— 未回填时必须降级到「不在现场」,
    绝不能抛,也绝不能瞎认。"""
    overlay("post_office", POST_OFFICE)
    assert duty_service.duty_venue_location_at(_postman((46, 103))) is None
    assert duty_service.nearest_duty_venue(_postman((75, 56))) is None


def test_no_duty_means_no_venue_anywhere(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    plain = _postman((46, 103), duty_key=None)
    assert duty_service.duty_venue_location_at(plain) is None
    assert duty_service.nearest_duty_venue(plain) is None


def test_nearest_duty_venue_finds_the_only_postal_place(overlay):
    overlay("post_office", POST_OFFICE, capabilities={CAP_POSTAL: {}})
    assert duty_service.nearest_duty_venue(_postman((75, 56))) == "post_office"
    # 全镇没有 postal 地点时(未 overlay)返回 None —— 见下一条。


def test_nearest_duty_venue_is_none_when_nothing_declares_postal():
    assert duty_service.nearest_duty_venue(_postman((75, 56))) is None


# ── 冷却键 ────────────────────────────────────────────────────────────

def test_cooldown_key_is_the_same_string_as_before():
    assert duty_service._duty_work_cooldown_key("abc") == "sv:duty_work:abc"


def test_no_bare_cooldown_literal_survives_outside_the_helper():
    offenders = []
    for i, line in enumerate(
            DUTY_SERVICE_SRC.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if "sv:duty_work:" in line and "_duty_work_cooldown_key" not in line:
            offenders.append(f"duty_service.py:{i}: {line.strip()}")
    assert len(offenders) == 1 and "def _duty_work_cooldown_key" not in offenders[0], (
        offenders)


@pytest.mark.anyio
async def test_duty_work_done_reads_the_cooldown_key():
    from app.redis_client import get_redis
    r = _postman()
    assert await duty_service.duty_work_done(r) is False
    await get_redis().set(duty_service._duty_work_cooldown_key(r.id), "1")
    assert await duty_service.duty_work_done(r) is True


@pytest.mark.anyio
async def test_duty_work_done_fails_closed_when_redis_is_down(monkeypatch):
    """Redis 抖动 → 视为已上工 → 不导流。宁可少一次导流,也不能因为 Redis 挂了
    把全镇有现场的营生持有人整齐赶去同一栋楼。"""
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(duty_service, "get_redis", _boom)
    assert await duty_service.duty_work_done(_postman()) is True


@pytest.mark.anyio
async def test_on_work_still_honors_the_same_cooldown_key(monkeypatch):
    """收敛后 on_work 与 duty_work_done 仍然读写同一个键(同串重构的机器证明)。"""
    from unittest.mock import AsyncMock
    from app.redis_client import get_redis

    r = _postman()
    handler = AsyncMock(return_value="done")
    monkeypatch.setitem(duty_service._WORK_HANDLERS, "postman", handler)
    monkeypatch.setattr(duty_service, "_pay_wage", AsyncMock())

    assert await duty_service.on_work(AsyncMock(), r) == "done"
    assert await get_redis().exists(duty_service._duty_work_cooldown_key(r.id))
    assert await duty_service.duty_work_done(r) is True
    assert await duty_service.on_work(AsyncMock(), r) is None  # 冷却生效
    assert handler.await_count == 1
