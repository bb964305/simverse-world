"""Regression tests for bounded planned-movement continuity."""
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.map_data import get_valid_target_tile
from app.agent.plan_continuity import (
    _ACTIVE_TTL_SECONDS,
    _active_key,
    active_trip_resident_ids,
    claim_slot_interrupt,
    get_active_trip,
    set_active_trip,
)
from app.config import settings
from app.redis_client import get_redis


def _trip(*, action="VISIT_DISTRICT", hour_range=(9, 12), steps=1):
    return {
        "action": action,
        "target": "academy",
        "target_tile": list(get_valid_target_tile("academy")),
        "plan_date": "2026-08-11",
        "plan_slot": 1,
        "plan_hour_range": list(hour_range),
        "location": "academy",
        "importance": 3,
        "reason": "上课",
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "step_count": steps,
    }


@pytest.fixture
def fixed_world(monkeypatch):
    from app import world_clock
    now = datetime(2026, 8, 11, 10, tzinfo=UTC)
    monkeypatch.setattr(world_clock, "now_world", lambda: now)
    monkeypatch.setattr(world_clock, "world_date_key", lambda: "2026-08-11")
    monkeypatch.setattr(settings, "realism_plan_continuation_max_steps", 32)


@pytest.mark.anyio
async def test_active_trip_is_real_time_bounded_and_validated(fixed_world):
    await set_active_trip("r1", _trip())

    assert (await get_active_trip("r1"))["target"] == "academy"
    ttl = await get_redis().ttl(_active_key("r1"))
    assert 0 < ttl <= _ACTIVE_TTL_SECONDS


@pytest.mark.anyio
@pytest.mark.parametrize(
    "update",
    [
        {"plan_date": "2026-08-10"},
        {"plan_hour_range": [7, 9]},
        {"action": "IDLE"},
        {"target": "missing", "location": "missing"},
        {"step_count": 32},
        {"step_count": "1"},
        {"started_at": (datetime.now(UTC) - timedelta(hours=3)).isoformat()},
    ],
)
async def test_invalid_or_stale_trip_is_cleared(fixed_world, update):
    trip = _trip()
    trip.update(update)
    await set_active_trip("r1", trip)

    assert await get_active_trip("r1") is None
    assert await get_redis().get(_active_key("r1")) is None


@pytest.mark.anyio
async def test_interrupt_debounce_is_per_reason(fixed_world):
    assert await claim_slot_interrupt("r1", "2026-08-11", 1, "spontaneous")
    assert not await claim_slot_interrupt("r1", "2026-08-11", 1, "spontaneous")
    # A failed spontaneous trial must not consume the social trial.
    assert await claim_slot_interrupt("r1", "2026-08-11", 1, "social_eager")
    assert await claim_slot_interrupt("r1", "2026-08-11", 1, "notable_event")


@pytest.mark.anyio
async def test_active_trip_existence_hint_is_batched_without_validation():
    await get_redis().set(_active_key("r1"), "not-yet-validated")
    assert await active_trip_resident_ids(["r1", "r2", "r1"]) == {"r1"}
