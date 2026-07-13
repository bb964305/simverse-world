"""E6 weather machine: transition matrix, season mapping, cron generation, prompt hint."""

import random
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.models.user import User  # noqa: F401 — FK metadata ordering
from app.models.resident import Resident
from app.models.world_event import WorldEvent
from app.tasks.weather import (
    TRANSITIONS,
    ensure_weather_event,
    sample_next_kind,
    sample_segment,
    season_for_month,
)


# ── transition matrix ────────────────────────────────────────────────

def test_matrix_rows_sum_to_one():
    for season, matrix in TRANSITIONS.items():
        for state, row in matrix.items():
            assert abs(sum(row.values()) - 1.0) < 1e-9, f"{season}/{state} row sums to {sum(row.values())}"


def test_storm_only_reachable_via_rain_or_storm():
    for season, matrix in TRANSITIONS.items():
        for state, row in matrix.items():
            if state not in ("rain", "storm"):
                assert "storm" not in row, f"{season}/{state} can reach storm directly"
        assert matrix["rain"].get("storm", 0) > 0, f"{season}: rain cannot reach storm"


def test_snow_only_in_winter_matrix():
    for season, matrix in TRANSITIONS.items():
        kinds = set(matrix) | {k for row in matrix.values() for k in row}
        if season == "winter":
            assert "snow" in kinds
        else:
            assert "snow" not in kinds


def test_10k_sampling_matches_matrix_distribution():
    """10k draws per row track the configured probabilities within ±2.5%."""
    rng = random.Random(42)
    n = 10_000
    for season, matrix in TRANSITIONS.items():
        for state, row in matrix.items():
            counts: dict[str, int] = {}
            for _ in range(n):
                k = sample_next_kind(state, season, rng)
                counts[k] = counts.get(k, 0) + 1
            assert set(counts) <= set(row), f"{season}/{state} produced out-of-row kind"
            for kind, prob in row.items():
                observed = counts.get(kind, 0) / n
                assert abs(observed - prob) < 0.025, (
                    f"{season}/{state}->{kind}: observed {observed:.3f}, expected {prob}"
                )


def test_10k_sampling_snow_only_in_winter_and_storm_reachable():
    rng = random.Random(7)
    summer_kinds = {sample_next_kind("rain", "summer", rng) for _ in range(10_000)}
    assert "storm" in summer_kinds  # rain -> storm reachable
    assert "snow" not in summer_kinds
    winter_kinds = {sample_next_kind("cloudy", "winter", rng) for _ in range(10_000)}
    assert "snow" in winter_kinds


def test_sample_segment_bounds():
    rng = random.Random(3)
    for _ in range(200):
        kind, intensity, hours = sample_segment("cloudy", "winter", rng)
        assert kind in {"sunny", "cloudy", "rain", "storm", "snow"}
        assert 0.0 <= intensity <= 1.0
        assert 2.0 <= hours <= 6.0


def test_unknown_prev_kind_falls_back():
    # snow row does not exist outside winter; must not KeyError.
    rng = random.Random(1)
    kind = sample_next_kind("snow", "summer", rng)
    assert kind in TRANSITIONS["summer"]["cloudy"]


# ── season mapping ───────────────────────────────────────────────────

def test_season_mapping_all_months():
    expected = {
        1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
        6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn",
        11: "autumn", 12: "winter",
    }
    for month, season in expected.items():
        assert season_for_month(month) == season


# ── cron generation ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_ensure_weather_creates_event_when_none(db_session):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)  # July -> summer
    event = await ensure_weather_event(db_session, now=now, rng=random.Random(5))

    assert event is not None
    assert event.type == "weather"
    assert event.is_active is False  # existing S1 cron flips + broadcasts it
    payload = event.payload_json
    assert payload["kind"] in {"sunny", "cloudy", "rain", "storm"}
    assert payload["season"] == "summer"
    assert 0.0 <= payload["intensity"] <= 1.0
    duration = event.ends_at - event.starts_at
    assert timedelta(hours=2) <= duration <= timedelta(hours=6)
    assert event.title and event.description


@pytest.mark.anyio
async def test_ensure_weather_skips_when_segment_pending(db_session):
    """Active or not-yet-flipped weather with ends_at in the future blocks a new draw."""
    now = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    for is_active in (True, False):
        db_session.add(WorldEvent(
            type="weather", title="下雨", description="雨",
            payload_json={"kind": "rain", "intensity": 0.5},
            starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=2),
            is_active=is_active,
        ))
        await db_session.commit()

        created = await ensure_weather_event(db_session, now=now)
        assert created is None

        count = len((await db_session.execute(
            select(WorldEvent).where(WorldEvent.type == "weather")
        )).scalars().all())
        assert count == 1

        # reset for the second iteration
        for e in (await db_session.execute(select(WorldEvent))).scalars().all():
            await db_session.delete(e)
        await db_session.commit()


@pytest.mark.anyio
async def test_ensure_weather_chains_from_previous_kind(db_session):
    """A new segment is drawn from the expired segment's kind (Markov chain)."""
    now = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)  # January -> winter
    db_session.add(WorldEvent(
        type="weather", title="下雪", description="雪",
        payload_json={"kind": "snow", "intensity": 0.6},
        starts_at=now - timedelta(hours=5), ends_at=now - timedelta(minutes=1),
        is_active=True,  # expired but not yet flipped off — must not block
    ))
    await db_session.commit()

    event = await ensure_weather_event(db_session, now=now, rng=random.Random(11))
    assert event is not None
    assert event.payload_json["season"] == "winter"
    # snow row in winter only leads to sunny/cloudy/snow
    assert event.payload_json["kind"] in set(TRANSITIONS["winter"]["snow"])


@pytest.mark.anyio
async def test_created_weather_event_flips_active_via_existing_cron(db_session):
    """Design choice: weather events are inserted inactive and the S1
    flip_active_events pass activates + reports them (broadcast payload)."""
    from app.services.world_event_service import flip_active_events

    # flip_active_events compares against the real clock — seed 1s in the past.
    event = await ensure_weather_event(db_session, now=datetime.now(UTC) - timedelta(seconds=1))
    assert event is not None and event.is_active is False

    changes = await flip_active_events(db_session)
    weather_changes = [(e, p) for e, p in changes if e["type"] == "weather"]
    assert len(weather_changes) == 1
    flipped, phase = weather_changes[0]
    assert phase == "start"
    assert flipped["payload_json"]["kind"] == event.payload_json["kind"]

    row = (await db_session.execute(
        select(WorldEvent).where(WorldEvent.type == "weather")
    )).scalars().one()
    assert row.is_active is True


# ── behavior influence ───────────────────────────────────────────────

def _decision_user_prompt(world_events):
    from app.agent.prompts import build_decision_prompt

    resident = Resident(slug="r1", name="小明", district="central_plaza", status="idle",
                        tile_x=0, tile_y=0, meta_json={})
    _system, user = build_decision_prompt(
        resident=resident, schedule_phase="day", world_time="10:00",
        nearby_residents=[], memories=[], today_actions=[], available_actions=[],
        max_daily_actions=10, world_events=world_events,
    )
    return user


def test_decide_prompt_rain_hint():
    user = _decision_user_prompt([
        {"type": "weather", "title": "下雨", "payload_json": {"kind": "rain", "intensity": 0.5}},
    ])
    assert "下雨，不太想出门" in user


def test_decide_prompt_storm_hint():
    user = _decision_user_prompt([
        {"type": "weather", "title": "暴风雨", "payload_json": {"kind": "storm", "intensity": 0.9}},
    ])
    assert "待在室内" in user


def test_decide_prompt_sunny_has_no_hint():
    user = _decision_user_prompt([
        {"type": "weather", "title": "晴天", "payload_json": {"kind": "sunny", "intensity": 0.0}},
    ])
    assert "不太想出门" not in user and "待在室内" not in user
    assert "当前世界事件：晴天" in user


def test_build_schedule_accepts_weather_without_changing_clock():
    """E6 design choice: the weather param must not alter the schedule."""
    from app.agent.scheduler import build_schedule

    base = build_schedule(None)
    rainy = build_schedule(None, weather={"kind": "storm", "intensity": 1.0})
    assert (base.wake_hour, base.sleep_hour, base.peak_hours, base.social_slots, base.rest_ratio) == \
           (rainy.wake_hour, rainy.sleep_hour, rainy.peak_hours, rainy.social_slots, rainy.rest_ratio)


@pytest.mark.anyio
async def test_get_current_weather_reads_active_event(db_session):
    from app.services import world_event_service as svc
    from app.tasks.weather import get_current_weather

    svc.invalidate_active_cache()
    now = datetime.now(UTC)
    db_session.add(WorldEvent(
        type="weather", title="下雨", description="雨",
        payload_json={"kind": "rain", "intensity": 0.7},
        starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
        is_active=True,
    ))
    await db_session.commit()

    payload = await get_current_weather(db_session)
    assert payload == {"kind": "rain", "intensity": 0.7}
    svc.invalidate_active_cache()
