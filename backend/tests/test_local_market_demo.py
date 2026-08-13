from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

import pytest

from scripts.local_market_demo import (
    CARAVAN_ROUTE,
    PhaseDurations,
    ResidentProjection,
    VISITOR_SLOTS,
    generate_cycle_frames,
    load_eligible_residents,
    sample_axis_path,
    validate_api_base_url,
    validate_redis_url,
)


def _residents() -> tuple[ResidentProjection, ...]:
    starts = ((110, 74), (106, 45), (50, 86), (75, 43))
    return tuple(
        ResidentProjection(
            slug=f"resident-{index}",
            name=f"Resident {index}",
            start=start,
            slot=slot,
            path=(
                start,
                (start[0], 92),
                (slot[0], 92),
                slot,
            ),
        )
        for index, (start, slot) in enumerate(zip(starts, VISITOR_SLOTS), start=1)
    )


def test_cycle_generator_emits_all_full_caravan_snapshots_in_order():
    started_at = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
    durations = PhaseDurations.from_round_seconds(60)
    frames = generate_cycle_frames(
        _residents(),
        started_at=started_at,
        durations=durations,
        visit_id="visit-local",
        world_event_id="event-local",
    )
    snapshots = [frame.data for frame in frames if frame.data["type"] == "caravan_state"]

    assert [snapshot["phase"] for snapshot in snapshots] == [
        "waiting", "inbound", "trading", "outbound", "departed",
    ]
    assert [snapshot["version"] for snapshot in snapshots] == [1, 2, 3, 4, 5]
    assert all(set(snapshot) == {
        "type", "visit_id", "world_event_id", "version", "phase",
        "server_time", "position", "motion", "summary", "visible",
    } for snapshot in snapshots)
    assert snapshots[0]["position"] == {"tile_x": 102, "tile_y": 127}
    assert snapshots[1]["motion"]["path"] == [list(tile) for tile in CARAVAN_ROUTE]
    assert snapshots[2]["position"] == {"tile_x": 109, "tile_y": 94}
    assert snapshots[3]["motion"]["path"] == [list(tile) for tile in reversed(CARAVAN_ROUTE)]
    assert snapshots[4]["visible"] is False


def test_cycle_sends_four_residents_to_unique_slots_and_back_home():
    residents = _residents()
    frames = generate_cycle_frames(
        residents,
        started_at=datetime(2026, 8, 13, tzinfo=UTC),
        durations=PhaseDurations.from_round_seconds(60),
        visit_id="visit-local",
        world_event_id="event-local",
    )
    moves = [frame.data for frame in frames if frame.data["type"] == "resident_move"]

    # This projection driver must never impersonate the authoritative economy
    # service. A real market_purchase frame carries durable purchase provenance.
    assert all(frame.data["type"] != "market_purchase" for frame in frames)

    for resident in residents:
        resident_moves = [move for move in moves if move["resident_slug"] == resident.slug]
        assert any((move["tile_x"], move["tile_y"]) == resident.slot for move in resident_moves)
        assert (resident_moves[-1]["tile_x"], resident_moves[-1]["tile_y"]) == resident.start
        assert resident_moves[0]["target_tile"] == list(resident.slot)
        assert resident_moves[-1]["target_tile"] == list(resident.start)

    inbound_end = 60 * (0.08 + 0.30)
    outbound_end = 60 * (0.08 + 0.30 + 0.30 + 0.30)
    inbound_moves = [
        frame for frame in frames
        if frame.data["type"] == "resident_move" and frame.offset_seconds < 60 * 0.68
    ]
    outbound_moves = [
        frame for frame in frames
        if frame.data["type"] == "resident_move" and frame.offset_seconds >= 60 * 0.68
    ]
    assert max(frame.offset_seconds for frame in inbound_moves) <= inbound_end - 0.85
    assert max(frame.offset_seconds for frame in outbound_moves) <= outbound_end - 0.85


def test_axis_path_sampling_preserves_every_corner():
    path = (
        (0, 0), (1, 0), (2, 0), (3, 0),
        (3, 1), (3, 2), (3, 3),
        (4, 3), (5, 3),
    )
    sampled = sample_axis_path(path, max_points=6)

    assert sampled[0] == path[0]
    assert sampled[-1] == path[-1]
    assert (3, 0) in sampled
    assert (3, 3) in sampled
    for current, nxt in zip(sampled, sampled[1:]):
        assert current[0] == nxt[0] or current[1] == nxt[1]


def test_round_duration_validation_and_total():
    durations = PhaseDurations.from_round_seconds(60)
    assert durations.total == pytest.approx(60)
    with pytest.raises(ValueError, match="round_seconds"):
        PhaseDurations.from_round_seconds(19)


@pytest.mark.parametrize(
    "url",
    [
        "redis://vm212:6379/0",
        "redis://user:secret@127.0.0.1:6379/0",
        "redis://127.0.0.1/0",
        "rediss://127.0.0.1:6379/0",
    ],
)
def test_redis_guard_rejects_remote_credentials_and_implicit_ports(url):
    with pytest.raises(ValueError):
        validate_redis_url(url)


def test_loopback_endpoint_guards_accept_explicit_local_ports():
    assert validate_redis_url("redis://127.0.0.1:6380/0") == ("127.0.0.1", 6380, "/0")
    assert validate_api_base_url("http://localhost:8000") == ("localhost", 8000, "/")
    with pytest.raises(ValueError, match="loopback"):
        validate_api_base_url("http://vm212:8000")


def test_resident_roster_is_read_from_sqlite_without_writes(tmp_path):
    database = tmp_path / "demo.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE residents (
            slug TEXT, name TEXT, tile_x INTEGER, tile_y INTEGER,
            resident_type TEXT, status TEXT
        )
        """
    )
    connection.executemany(
        "INSERT INTO residents VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("awake", "Awake", 1, 2, "npc", "idle"),
            ("ugc", "UGC", 3, 4, "resident", "walking"),
            ("asleep", "Asleep", 5, 6, "npc", "sleeping"),
            ("avatar", "Avatar", 7, 8, "player", "idle"),
        ],
    )
    connection.commit()
    connection.close()

    assert load_eligible_residents(database) == (
        ("awake", "Awake", (1, 2)),
        ("ugc", "UGC", (3, 4)),
    )
