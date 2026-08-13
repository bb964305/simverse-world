#!/usr/bin/env python3
"""Local-only caravan + four-resident market animation driver.

This script is intentionally separated from the production lifecycle:

* it never imports application settings or reads ``.env``;
* it opens one explicitly named SQLite database in read-only mode;
* it refuses non-loopback HTTP/Redis endpoints and Redis credentials;
* it only publishes ephemeral WebSocket frames through local Redis;
* it defaults to a dry run. ``--run`` plus explicit local endpoints is required
  before anything is published.

The database remains authoritative and unchanged. On normal completion,
SIGINT, or SIGTERM, the driver publishes a terminal caravan snapshot and moves
the four projected residents back to their database positions.

Example (run from ``backend/`` after reviewing the dry run)::

    python scripts/local_market_demo.py \
      --db /path/to/local-demo.db --round-seconds 60

    python scripts/local_market_demo.py --run \
      --db /path/to/local-demo.db \
      --api-base-url http://127.0.0.1:8000 \
      --redis-url redis://127.0.0.1:6380/0 \
      --round-seconds 60 --cycles 0
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import itertools
import json
from pathlib import Path
import signal
import sqlite3
import sys
from typing import Awaitable, Callable, Iterable, Sequence
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler
import uuid


WS_CHANNEL = "sv:ws"
VISITOR_SLOTS: tuple[tuple[int, int], ...] = (
    (114, 93),
    (116, 93),
    (114, 95),
    (116, 95),
)
CARAVAN_ROUTE: tuple[tuple[int, int], ...] = tuple(
    [(102, y) for y in range(127, 93, -1)]
    + [(x, 94) for x in range(103, 110)]
)
SUMMARY = {
    "fee_sc": 0,
    "bought": 0,
    "spent_sc": 0,
    "tax_sc": 0,
    "imports_stocked": 0,
}
AUTONOMOUS_TYPES = ("npc", "resident")
BUSY_STATUSES = ("sleeping", "chatting", "socializing")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MIN_MOVE_INTERVAL_SECONDS = 0.85


Tile = tuple[int, int]


@dataclass(frozen=True)
class ResidentProjection:
    slug: str
    name: str
    start: Tile
    slot: Tile
    path: tuple[Tile, ...]


@dataclass(frozen=True)
class PhaseDurations:
    waiting: float
    inbound: float
    trading: float
    outbound: float
    departed: float

    @classmethod
    def from_round_seconds(cls, seconds: float) -> "PhaseDurations":
        if not 20 <= seconds <= 3600:
            raise ValueError("round_seconds must be between 20 and 3600")
        # Travel receives enough of the round for collision-safe resident
        # waypoints; trading remains long enough to inspect the four visitors.
        return cls(
            waiting=seconds * 0.08,
            inbound=seconds * 0.30,
            trading=seconds * 0.30,
            outbound=seconds * 0.30,
            departed=seconds * 0.02,
        )

    @property
    def total(self) -> float:
        return self.waiting + self.inbound + self.trading + self.outbound + self.departed


@dataclass(frozen=True)
class ScheduledFrame:
    offset_seconds: float
    data: dict


@dataclass
class PublishedState:
    visit_id: str | None = None
    world_event_id: str | None = None
    version: int = 0


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _snapshot(
    *,
    visit_id: str,
    world_event_id: str,
    version: int,
    phase: str,
    server_time: datetime,
    position: Tile,
    motion_path: Sequence[Tile] | None = None,
    motion_started_at: datetime | None = None,
    motion_ends_at: datetime | None = None,
) -> dict:
    motion = None
    if motion_path is not None:
        if motion_started_at is None or motion_ends_at is None:
            raise ValueError("moving snapshots require motion timestamps")
        motion = {
            "path": [[x, y] for x, y in motion_path],
            "started_at": _iso(motion_started_at),
            "ends_at": _iso(motion_ends_at),
        }
    return {
        "type": "caravan_state",
        "visit_id": visit_id,
        "world_event_id": world_event_id,
        "version": version,
        "phase": phase,
        "server_time": _iso(server_time),
        "position": {"tile_x": position[0], "tile_y": position[1]},
        "motion": motion,
        "summary": dict(SUMMARY),
        "visible": phase in {"waiting", "inbound", "trading", "outbound"},
    }


def _path_turn_indexes(path: Sequence[Tile]) -> set[int]:
    required = {0, len(path) - 1}
    prior_direction: Tile | None = None
    for index, (current, nxt) in enumerate(zip(path, path[1:]), start=0):
        direction = (nxt[0] - current[0], nxt[1] - current[1])
        if prior_direction is not None and direction != prior_direction:
            required.add(index)
        prior_direction = direction
    return required


def sample_axis_path(path: Sequence[Tile], max_points: int) -> tuple[Tile, ...]:
    """Reduce a 4-neighbour path without cutting any authored corner.

    Every retained segment stays on a single source-path row or column. Extra
    capacity is spent bisecting the longest straight runs, making long travel
    less jumpy while preserving the collision-safe route.
    """
    if not path:
        raise ValueError("resident path cannot be empty")
    if max_points < 2 and len(path) > 1:
        raise ValueError("max_points must be at least two")
    if len(path) == 1:
        return (path[0],)

    selected = _path_turn_indexes(path)
    if len(selected) > max_points:
        raise ValueError(
            f"phase is too short for {len(selected)} collision-safe path corners; "
            "increase --round-seconds"
        )
    while len(selected) < min(max_points, len(path)):
        ordered = sorted(selected)
        left, right = max(
            zip(ordered, ordered[1:]),
            key=lambda pair: pair[1] - pair[0],
        )
        if right - left <= 1:
            break
        selected.add((left + right) // 2)
    return tuple(path[index] for index in sorted(selected))


def _resident_frames(
    residents: Sequence[ResidentProjection],
    *,
    phase_start: float,
    phase_seconds: float,
    outbound: bool,
) -> list[ScheduledFrame]:
    max_points = max(2, int(phase_seconds / _MIN_MOVE_INTERVAL_SECONDS))
    frames: list[ScheduledFrame] = []
    for resident in residents:
        full_path = tuple(reversed(resident.path)) if outbound else resident.path
        path = sample_axis_path(full_path, max_points=max_points)
        if len(path) == 1:
            continue
        # Leave one complete frontend tween interval before the caravan phase
        # transition. This prevents the last arrival/home tween from bleeding
        # into trading or the following cycle, even at the 20-second minimum.
        usable = max(0.1, phase_seconds - _MIN_MOVE_INTERVAL_SECONDS)
        step = usable / (len(path) - 1)
        for index, (tile_x, tile_y) in enumerate(path[1:], start=1):
            frames.append(ScheduledFrame(
                phase_start + step * index,
                {
                    "type": "resident_move",
                    "resident_slug": resident.slug,
                    "tile_x": tile_x,
                    "tile_y": tile_y,
                    "target_tile": list(resident.start if outbound else resident.slot),
                    "status": "walking",
                },
            ))
    return frames


def generate_cycle_frames(
    residents: Sequence[ResidentProjection],
    *,
    started_at: datetime,
    durations: PhaseDurations,
    visit_id: str,
    world_event_id: str,
    caravan_route: Sequence[Tile] = CARAVAN_ROUTE,
) -> tuple[ScheduledFrame, ...]:
    """Generate one complete, side-effect-free market demo timeline."""
    if len(residents) != len(VISITOR_SLOTS):
        raise ValueError("exactly four resident projections are required")
    if {resident.slot for resident in residents} != set(VISITOR_SLOTS):
        raise ValueError("residents must occupy the four distinct visitor slots")
    if len({resident.slug for resident in residents}) != len(residents):
        raise ValueError("resident slugs must be unique")
    if len(caravan_route) < 2:
        raise ValueError("caravan route must contain at least two tiles")
    if started_at.tzinfo is None:
        raise ValueError("started_at must be timezone-aware")

    waiting_at = 0.0
    inbound_at = durations.waiting
    trading_at = inbound_at + durations.inbound
    outbound_at = trading_at + durations.trading
    departed_at = outbound_at + durations.outbound
    outside = tuple(caravan_route[0])
    market = tuple(caravan_route[-1])
    inbound_start = started_at + timedelta(seconds=inbound_at)
    trading_start = started_at + timedelta(seconds=trading_at)
    outbound_start = started_at + timedelta(seconds=outbound_at)
    departed_start = started_at + timedelta(seconds=departed_at)

    frames: list[ScheduledFrame] = [
        ScheduledFrame(waiting_at, _snapshot(
            visit_id=visit_id,
            world_event_id=world_event_id,
            version=1,
            phase="waiting",
            server_time=started_at,
            position=outside,
        )),
        ScheduledFrame(inbound_at, _snapshot(
            visit_id=visit_id,
            world_event_id=world_event_id,
            version=2,
            phase="inbound",
            server_time=inbound_start,
            position=outside,
            motion_path=caravan_route,
            motion_started_at=inbound_start,
            motion_ends_at=trading_start,
        )),
    ]
    frames.extend(_resident_frames(
        residents,
        phase_start=inbound_at,
        phase_seconds=durations.inbound,
        outbound=False,
    ))
    frames.append(ScheduledFrame(trading_at, _snapshot(
        visit_id=visit_id,
        world_event_id=world_event_id,
        version=3,
        phase="trading",
        server_time=trading_start,
        position=market,
    )))
    frames.extend(ScheduledFrame(
        trading_at + 0.01,
        {
            "type": "resident_status",
            "resident_slug": resident.slug,
            "status": "idle",
        },
    ) for resident in residents)
    frames.append(ScheduledFrame(outbound_at, _snapshot(
        visit_id=visit_id,
        world_event_id=world_event_id,
        version=4,
        phase="outbound",
        server_time=outbound_start,
        position=market,
        motion_path=tuple(reversed(caravan_route)),
        motion_started_at=outbound_start,
        motion_ends_at=departed_start,
    )))
    frames.extend(_resident_frames(
        residents,
        phase_start=outbound_at,
        phase_seconds=durations.outbound,
        outbound=True,
    ))
    frames.append(ScheduledFrame(departed_at, _snapshot(
        visit_id=visit_id,
        world_event_id=world_event_id,
        version=5,
        phase="departed",
        server_time=departed_start,
        position=outside,
    )))
    frames.extend(ScheduledFrame(
        departed_at + 0.01,
        {
            "type": "resident_status",
            "resident_slug": resident.slug,
            "status": "idle",
        },
    ) for resident in residents)

    # Python's sort is stable, preserving caravan-before-resident ordering when
    # multiple transitions share the same scheduled timestamp.
    return tuple(sorted(frames, key=lambda frame: frame.offset_seconds))


def _sqlite_ro_uri(path: Path) -> str:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"database is not a file: {resolved}")
    return f"file:{resolved.as_posix()}?mode=ro"


def load_eligible_residents(path: Path) -> tuple[tuple[str, str, Tile], ...]:
    """Read eligible autonomous residents without ever opening a write handle."""
    try:
        connection = sqlite3.connect(_sqlite_ro_uri(path), uri=True)
        rows = connection.execute(
            """
            SELECT slug, name, tile_x, tile_y
            FROM residents
            WHERE resident_type IN (?, ?)
              AND status NOT IN (?, ?, ?)
            ORDER BY slug
            """,
            (*AUTONOMOUS_TYPES, *BUSY_STATUSES),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"could not read local resident roster: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()
    return tuple(
        (str(slug), str(name), (int(tile_x), int(tile_y)))
        for slug, name, tile_x, tile_y in rows
    )


def _validate_local_url(value: str, *, scheme: str, label: str) -> tuple[str, int, str]:
    parsed = urlsplit(value)
    if parsed.scheme != scheme:
        raise ValueError(f"{label} must use {scheme}://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} credentials are forbidden in this local demo")
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError(f"{label} must target a loopback host")
    if parsed.port is None:
        raise ValueError(f"{label} must include an explicit port")
    return parsed.hostname, parsed.port, parsed.path or "/"


def validate_redis_url(value: str) -> tuple[str, int, str]:
    host, port, path = _validate_local_url(value, scheme="redis", label="Redis URL")
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment:
        raise ValueError("Redis URL query/fragment is not allowed")
    return host, port, path


def validate_api_base_url(value: str) -> tuple[str, int, str]:
    host, port, path = _validate_local_url(value, scheme="http", label="API base URL")
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment:
        raise ValueError("API base URL query/fragment is not allowed")
    if path not in {"", "/"}:
        raise ValueError("API base URL must not include a path")
    return host, port, path


def verify_api_roster(api_base_url: str, slugs: Iterable[str]) -> None:
    """Ensure the browser-facing backend actually exposes all chosen sprites."""
    validate_api_base_url(api_base_url)
    endpoint = api_base_url.rstrip("/") + "/residents?limit=500"
    try:
        # Ignore HTTP(S)_PROXY from the developer shell. The URL guard above
        # guarantees loopback; routing this health check through a proxy would
        # both weaken that guarantee and make local demos environment-dependent.
        opener = build_opener(ProxyHandler({}))
        with opener.open(endpoint, timeout=3) as response:  # noqa: S310 - loopback-only above
            payload = json.load(response)
    except Exception as exc:
        raise ValueError("could not read the loopback API resident roster") from exc
    if not isinstance(payload, list):
        raise ValueError("loopback API returned an invalid resident roster")
    visible = {
        str(item.get("slug"))
        for item in payload
        if isinstance(item, dict) and item.get("slug")
    }
    missing = sorted(set(slugs) - visible)
    if missing:
        raise ValueError(
            "chosen residents are not loaded by the browser-facing API: "
            + ", ".join(missing)
        )


def build_resident_projections(
    roster: Sequence[tuple[str, str, Tile]],
    requested_slugs: Sequence[str] = (),
) -> tuple[ResidentProjection, ...]:
    """Choose four nearby residents and assign collision-safe visitor paths."""
    if len(set(requested_slugs)) != len(requested_slugs):
        raise ValueError("--resident-slug values must be unique")
    if requested_slugs and len(requested_slugs) != 4:
        raise ValueError("provide exactly four --resident-slug values")

    # Imported lazily: the script never loads app settings or database config.
    from app.agent.pathfinder import find_path, get_walkable_tiles

    by_slug = {slug: (slug, name, start) for slug, name, start in roster}
    if requested_slugs:
        missing = sorted(set(requested_slugs) - set(by_slug))
        if missing:
            raise ValueError("requested residents are unavailable: " + ", ".join(missing))
        candidates = [by_slug[slug] for slug in requested_slugs]
    else:
        candidates = list(roster)
    if len(candidates) < 4:
        raise ValueError(
            f"local demo database has {len(candidates)} eligible autonomous residents; four are required"
        )

    walkable = get_walkable_tiles()
    route_cache: dict[tuple[str, Tile], tuple[Tile, ...]] = {}
    for slug, _name, start in candidates:
        for slot in VISITOR_SLOTS:
            path = find_path(start, slot, walkable)
            if path:
                route_cache[(slug, slot)] = tuple(path)

    if not requested_slugs:
        reachable = [
            resident for resident in candidates
            if any((resident[0], slot) in route_cache for slot in VISITOR_SLOTS)
        ]
        candidates = sorted(
            reachable,
            key=lambda resident: (
                min(len(route_cache[(resident[0], slot)])
                    for slot in VISITOR_SLOTS if (resident[0], slot) in route_cache),
                resident[0],
            ),
        )[:4]
    if len(candidates) != 4:
        raise ValueError("fewer than four eligible residents can reach the market visitor slots")

    best: tuple[int, tuple[Tile, ...]] | None = None
    for slots in itertools.permutations(VISITOR_SLOTS):
        paths: list[tuple[Tile, ...]] = []
        for resident, slot in zip(candidates, slots):
            path = route_cache.get((resident[0], slot))
            if path is None:
                break
            paths.append(path)
        if len(paths) != 4:
            continue
        score = sum(len(path) for path in paths)
        if best is None or score < best[0]:
            best = (score, slots)
    if best is None:
        raise ValueError("could not assign four distinct reachable market visitor slots")

    assignments: list[ResidentProjection] = []
    for (slug, name, start), slot in zip(candidates, best[1]):
        assignments.append(ResidentProjection(
            slug=slug,
            name=name,
            start=start,
            slot=slot,
            path=route_cache[(slug, slot)],
        ))
    return tuple(assignments)


class RedisPublisher:
    def __init__(self, redis_url: str):
        validate_redis_url(redis_url)
        self.redis_url = redis_url
        self.client = None

    async def __aenter__(self) -> "RedisPublisher":
        import redis.asyncio as aioredis

        self.client = aioredis.from_url(self.redis_url, decode_responses=True)
        await self.client.ping()
        subscribers = await self.client.pubsub_numsub(WS_CHANNEL)
        subscriber_count = int(subscribers[0][1]) if subscribers else 0
        if subscriber_count < 1:
            await self.client.aclose()
            self.client = None
            raise RuntimeError("local Redis has no sv:ws subscriber; is the backend running?")
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def publish(self, data: dict) -> None:
        if self.client is None:
            raise RuntimeError("Redis publisher is not connected")
        envelope = {"op": "broadcast", "data": data, "exclude": None}
        await self.client.publish(WS_CHANNEL, json.dumps(envelope, ensure_ascii=False))


async def _wait_until(stop: asyncio.Event, deadline: float) -> bool:
    delay = deadline - asyncio.get_running_loop().time()
    if delay <= 0:
        return stop.is_set()
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
        return True
    except TimeoutError:
        return False


async def publish_cleanup(
    publish: Callable[[dict], Awaitable[None]],
    residents: Sequence[ResidentProjection],
    state: PublishedState,
) -> None:
    now = datetime.now(UTC)
    visit_id = state.visit_id or f"local-market-cleanup-{uuid.uuid4().hex[:12]}"
    world_event_id = state.world_event_id or f"local-market-cleanup-event-{uuid.uuid4().hex[:12]}"
    await publish(_snapshot(
        visit_id=visit_id,
        world_event_id=world_event_id,
        version=state.version + 1,
        phase="departed",
        server_time=now,
        position=CARAVAN_ROUTE[0],
    ))
    for resident in residents:
        await publish({
            "type": "resident_move",
            "resident_slug": resident.slug,
            "tile_x": resident.start[0],
            "tile_y": resident.start[1],
            "target_tile": list(resident.start),
            "status": "walking",
        })
        await publish({
            "type": "resident_status",
            "resident_slug": resident.slug,
            "status": "idle",
        })


async def run_demo(
    publish: Callable[[dict], Awaitable[None]],
    residents: Sequence[ResidentProjection],
    *,
    durations: PhaseDurations,
    cycles: int,
    stop: asyncio.Event,
) -> None:
    if cycles < 0:
        raise ValueError("cycles must be zero (infinite) or a positive integer")
    session_id = uuid.uuid4().hex[:12]
    state = PublishedState()
    cycle_number = 0
    try:
        while not stop.is_set() and (cycles == 0 or cycle_number < cycles):
            cycle_number += 1
            started_at = datetime.now(UTC)
            state.visit_id = f"local-market-demo-{session_id}-{cycle_number:04d}"
            state.world_event_id = f"local-market-demo-event-{session_id}-{cycle_number:04d}"
            state.version = 0
            frames = generate_cycle_frames(
                residents,
                started_at=started_at,
                durations=durations,
                visit_id=state.visit_id,
                world_event_id=state.world_event_id,
            )
            cycle_clock = asyncio.get_running_loop().time()
            for frame in frames:
                if await _wait_until(stop, cycle_clock + frame.offset_seconds):
                    break
                await publish(frame.data)
                if frame.data.get("type") == "caravan_state":
                    state.version = int(frame.data["version"])
            if stop.is_set():
                break
            await _wait_until(stop, cycle_clock + durations.total)
    finally:
        await asyncio.shield(publish_cleanup(publish, residents, state))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="publish to local Redis (without this flag the command is read-only dry-run)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "skills_world_dev.db",
        help="explicit local SQLite resident database (opened read-only)",
    )
    parser.add_argument(
        "--api-base-url",
        help="browser-facing loopback API, required with --run",
    )
    parser.add_argument(
        "--redis-url",
        help="credential-free loopback Redis URL with explicit port, required with --run",
    )
    parser.add_argument("--round-seconds", type=float, default=60.0)
    parser.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="number of cycles; 0 loops until SIGINT/SIGTERM",
    )
    parser.add_argument(
        "--resident-slug",
        action="append",
        default=[],
        help="pin one resident; when used, repeat exactly four times",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    durations = PhaseDurations.from_round_seconds(args.round_seconds)
    roster = load_eligible_residents(args.db)
    residents = build_resident_projections(roster, args.resident_slug)
    # Generate before publishing so a too-short round fails without side effects.
    frames = generate_cycle_frames(
        residents,
        started_at=datetime.now(UTC),
        durations=durations,
        visit_id="local-market-demo-dry-run",
        world_event_id="local-market-demo-event-dry-run",
    )

    print("mode=" + ("run" if args.run else "dry-run"))
    print(f"database={args.db.expanduser().resolve()}")
    print(f"round_seconds={durations.total:.1f} cycles={args.cycles}")
    print("phases=waiting,inbound,trading,outbound,departed")
    print("purchase_frames=0 (authoritative purchase service only)")
    for resident in residents:
        print(
            f"resident={resident.slug} start={resident.start} "
            f"slot={resident.slot} path_tiles={len(resident.path)}"
        )
    print(f"scheduled_frames={len(frames)}")

    if not args.run:
        print("published=0 (pass --run with explicit loopback API and Redis endpoints)")
        return 0
    if not args.api_base_url or not args.redis_url:
        raise ValueError("--run requires --api-base-url and --redis-url")
    validate_api_base_url(args.api_base_url)
    validate_redis_url(args.redis_url)
    verify_api_roster(args.api_base_url, (resident.slug for resident in residents))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows fallback
            pass

    redis_host, redis_port, redis_db = validate_redis_url(args.redis_url)
    print(f"redis={redis_host}:{redis_port}{redis_db} channel={WS_CHANNEL}")
    print("status=running; press Ctrl-C to publish cleanup and stop")
    async with RedisPublisher(args.redis_url) as publisher:
        await run_demo(
            publisher.publish,
            residents,
            durations=durations,
            cycles=args.cycles,
            stop=stop,
        )
    print("status=stopped-and-cleaned")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.cycles < 0:
            raise ValueError("--cycles must be zero or positive")
        return asyncio.run(_async_main(args))
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
