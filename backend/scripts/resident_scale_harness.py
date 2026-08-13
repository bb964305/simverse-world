#!/usr/bin/env python3
"""Deterministic 25–40 resident AgentLoop scalability harness.

This is deliberately a non-production harness:

* it uses a disposable SQLite database;
* it drives the real ``AgentLoop._tick_round`` fan-out/session/semaphore path;
* it replaces ``resident_tick`` with a deterministic actor that never reaches
  an external LLM, Redis, chat, or any production service;
* it reports elapsed time, action/broadcast counts and persisted DB effects;
* it validates count and consistency invariants, never a wall-clock threshold.

Run both roadmap target sizes from ``backend/``::

    python scripts/resident_scale_harness.py

The JSON output is suitable for attaching to CI or burn-in evidence.  A non-zero
exit status means at least one conservative invariant failed.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Running the script directly must not inherit production endpoints or require
# a developer .env.  These values are process-local and the harness creates its
# own engine below; they do not change application defaults.
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("LLM_API_KEY", "resident-scale-harness-disabled")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event, func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agent.actions import ActionResult, ActionType  # noqa: E402
from app.agent.loop import AgentLoop  # noqa: E402
from app.agent.scheduler import DailySchedule  # noqa: E402
from app.database import Base  # noqa: E402
from app.llm.budget import BudgetTier  # noqa: E402
from app.models.resident import Resident  # noqa: E402
from app.models.user import User  # noqa: E402


TARGET_RESIDENT_COUNTS = (25, 40)
_ACTION_CYCLE = (ActionType.WANDER, ActionType.IDLE, ActionType.WORK)
_SCHEDULE = DailySchedule(
    wake_hour=7,
    sleep_hour=23,
    peak_hours=[10, 15],
    social_slots=[12, 19],
    rest_ratio=0.3,
)


@dataclass
class _Tracker:
    attempted_ticks: int = 0
    in_flight: int = 0
    max_concurrency: int = 0
    llm_attempts: int = 0
    action_counts: Counter[str] = field(default_factory=Counter)
    resident_slugs: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScaleRunReport:
    resident_count: int
    rounds: int
    configured_concurrency: int
    elapsed_seconds: float
    expected_ticks: int
    attempted_ticks: int
    unique_residents_ticked: int
    max_concurrency: int
    action_counts: dict[str, int]
    broadcast_counts: dict[str, int]
    db_resident_count: int
    db_residents_updated: int
    db_persisted_ticks: int
    db_moved_residents: int
    external_llm_calls: int
    errors: tuple[str, ...]
    invariant_failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.invariant_failures

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["passed"] = self.passed
        return payload


def _resident(index: int) -> Resident:
    return Resident(
        id=f"scale-resident-{index:02d}",
        slug=f"scale-resident-{index:02d}",
        name=f"Scale Resident {index:02d}",
        creator_id="scale-harness-system",
        resident_type="npc",
        district="central_plaza",
        status="idle",
        tile_x=70 + (index % 8),
        tile_y=45 + (index // 8),
        meta_json={
            "scale_harness": {"index": index, "ticks": 0},
            "sbti": {
                "type": "GOGO",
                "dimensions": {
                    "Ac1": "M",
                    "Ac3": "M",
                    "So1": "M",
                    "E2": "M",
                    "A3": "M",
                },
            },
        },
    )


async def _seed(session_factory, resident_count: int) -> None:
    async with session_factory() as db:
        db.add(
            User(
                id="scale-harness-system",
                name="Scale Harness",
                email="scale-harness@invalid.example",
                soul_coin_balance=0,
            )
        )
        db.add_all(_resident(index) for index in range(resident_count))
        await db.commit()


def _install_sqlite_pragmas(engine) -> None:
    """Make short concurrent writes deterministic without production tuning."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


def _actor(tracker: _Tracker):
    async def deterministic_tick(
        db: AsyncSession,
        resident: Resident,
        *,
        force_plan_only: bool = False,
    ) -> ActionResult:
        del force_plan_only
        tracker.attempted_ticks += 1
        tracker.in_flight += 1
        tracker.max_concurrency = max(tracker.max_concurrency, tracker.in_flight)
        try:
            # Yield once so the real AgentLoop semaphore is exercised without
            # introducing a sleep-duration assertion.
            await asyncio.sleep(0)
            state = dict((resident.meta_json or {}).get("scale_harness") or {})
            index = int(state["index"])
            prior_ticks = int(state.get("ticks", 0))
            action = _ACTION_CYCLE[(index + prior_ticks) % len(_ACTION_CYCLE)]

            if action == ActionType.WANDER:
                resident.tile_x += 1
            state["ticks"] = prior_ticks + 1
            state["last_action"] = action.value
            resident.meta_json = {
                **(resident.meta_json or {}),
                "scale_harness": state,
            }
            await db.commit()

            tracker.action_counts[action.value] += 1
            tracker.resident_slugs.add(resident.slug)
            return ActionResult(
                action=action,
                target_slug=None,
                target_tile=(resident.tile_x, resident.tile_y)
                if action == ActionType.WANDER
                else None,
                reason="deterministic scale harness",
            )
        except Exception as exc:
            tracker.errors.append(f"{resident.slug}: {type(exc).__name__}: {exc}")
            raise
        finally:
            tracker.in_flight -= 1

    return deterministic_tick


async def _database_effects(session_factory, rounds: int) -> dict[str, int]:
    async with session_factory() as db:
        residents = (
            await db.execute(select(Resident).order_by(Resident.slug))
        ).scalars().all()
        persisted_ticks = 0
        updated = 0
        moved = 0
        for resident in residents:
            state = (resident.meta_json or {}).get("scale_harness") or {}
            ticks = int(state.get("ticks", 0))
            persisted_ticks += ticks
            updated += int(ticks == rounds)
            index = int(state.get("index", -1))
            initial_x = 70 + (index % 8)
            moved += int(resident.tile_x != initial_x)
        count = int(
            (await db.execute(select(func.count()).select_from(Resident))).scalar_one()
        )
    return {
        "resident_count": count,
        "residents_updated": updated,
        "persisted_ticks": persisted_ticks,
        "moved_residents": moved,
    }


def _invariant_failures(
    *,
    resident_count: int,
    rounds: int,
    concurrency: int,
    tracker: _Tracker,
    broadcasts: Counter[str],
    effects: dict[str, int],
) -> tuple[str, ...]:
    expected = resident_count * rounds
    failures: list[str] = []

    checks = {
        "attempted tick count": tracker.attempted_ticks == expected,
        "action count": sum(tracker.action_counts.values()) == expected,
        "all residents ticked": len(tracker.resident_slugs) == resident_count,
        "resident cardinality unchanged": effects["resident_count"] == resident_count,
        "every resident persisted every round": effects["residents_updated"] == resident_count,
        "persisted tick count": effects["persisted_ticks"] == expected,
        "semaphore concurrency cap": 0 < tracker.max_concurrency <= concurrency,
        "no actor errors": not tracker.errors,
        "movement broadcast count":
            broadcasts["resident_move"] == tracker.action_counts[ActionType.WANDER.value],
        "status broadcast count":
            broadcasts["resident_status"] == tracker.action_counts[ActionType.IDLE.value],
        "only expected broadcasts":
            set(broadcasts).issubset({"resident_move", "resident_status"}),
        "zero external LLM attempts": tracker.llm_attempts == 0,
    }
    for label, passed in checks.items():
        if not passed:
            failures.append(label)
    return tuple(failures)


async def run_scale_scenario(
    resident_count: int,
    *,
    rounds: int = 2,
    concurrency: int = 8,
    database_path: str | Path | None = None,
) -> ScaleRunReport:
    """Run one isolated scenario and return machine-readable evidence."""
    if not 1 <= resident_count <= 100:
        raise ValueError("resident_count must be between 1 and 100")
    if rounds < 1:
        raise ValueError("rounds must be at least 1")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    owned_tmpdir: tempfile.TemporaryDirectory[str] | None = None
    if database_path is None:
        owned_tmpdir = tempfile.TemporaryDirectory(prefix="simverse-scale-")
        database_path = Path(owned_tmpdir.name) / "scale.db"
    else:
        database_path = Path(database_path)

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )
    _install_sqlite_pragmas(engine)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    tracker = _Tracker()
    broadcasts: Counter[str] = Counter()

    async def record_broadcast(payload: dict, **_kwargs) -> None:
        broadcasts[str(payload.get("type") or "unknown")] += 1

    def forbid_llm(*_args, **_kwargs):
        tracker.llm_attempts += 1
        raise AssertionError("LLM access is forbidden in scale harness")

    async def forbid_llm_async(*_args, **_kwargs):
        return forbid_llm()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await _seed(session_factory, resident_count)

        harness_settings = SimpleNamespace(
            agent_max_concurrent=concurrency,
            realism_enabled=False,
            chat_engaged_tick_skip_enabled=False,
        )
        with ExitStack() as stack:
            stack.enter_context(patch("app.agent.loop.async_session", session_factory))
            stack.enter_context(
                patch(
                    "app.agent.loop.background_tier",
                    AsyncMock(return_value=BudgetTier.NORMAL),
                )
            )
            stack.enter_context(patch("app.agent.loop.resident_tick", new=_actor(tracker)))
            stack.enter_context(patch("app.agent.loop.settings", harness_settings))
            stack.enter_context(patch("app.agent.loop.build_schedule", return_value=_SCHEDULE))
            stack.enter_context(
                patch("app.agent.loop.get_activity_probability", return_value=0.8)
            )
            stack.enter_context(patch("app.agent.loop.should_tick", return_value=True))
            stack.enter_context(patch("app.world_clock.world_hour", return_value=12))
            stack.enter_context(patch("app.world_clock.world_weekday", return_value=2))
            stack.enter_context(
                patch("app.tasks.weather.get_current_weather", AsyncMock(return_value=None))
            )
            stack.enter_context(
                patch(
                    "app.services.world_event_service.get_active_events_cached",
                    AsyncMock(return_value=[]),
                )
            )
            stack.enter_context(
                patch("app.agent.loop.manager.broadcast", new=record_broadcast)
            )
            # Fail closed if a future AgentLoop change accidentally bypasses the
            # deterministic actor and tries to construct/call an LLM client.
            stack.enter_context(
                patch(
                    "app.llm.client.get_client",
                    side_effect=forbid_llm,
                )
            )
            stack.enter_context(
                patch(
                    "app.llm.client.chat",
                    AsyncMock(side_effect=forbid_llm_async),
                )
            )

            started = time.perf_counter()
            loop = AgentLoop()
            for _ in range(rounds):
                tier = await loop._tick_round()
                if tier != BudgetTier.NORMAL:
                    tracker.errors.append(f"unexpected budget tier: {tier}")
            elapsed = time.perf_counter() - started

        effects = await _database_effects(session_factory, rounds)
        failures = _invariant_failures(
            resident_count=resident_count,
            rounds=rounds,
            concurrency=concurrency,
            tracker=tracker,
            broadcasts=broadcasts,
            effects=effects,
        )
        return ScaleRunReport(
            resident_count=resident_count,
            rounds=rounds,
            configured_concurrency=concurrency,
            elapsed_seconds=round(elapsed, 6),
            expected_ticks=resident_count * rounds,
            attempted_ticks=tracker.attempted_ticks,
            unique_residents_ticked=len(tracker.resident_slugs),
            max_concurrency=tracker.max_concurrency,
            action_counts=dict(sorted(tracker.action_counts.items())),
            broadcast_counts=dict(sorted(broadcasts.items())),
            db_resident_count=effects["resident_count"],
            db_residents_updated=effects["residents_updated"],
            db_persisted_ticks=effects["persisted_ticks"],
            db_moved_residents=effects["moved_residents"],
            external_llm_calls=tracker.llm_attempts,
            errors=tuple(tracker.errors),
            invariant_failures=failures,
        )
    finally:
        await engine.dispose()
        if owned_tmpdir is not None:
            owned_tmpdir.cleanup()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic, zero-external-LLM resident scale scenarios."
    )
    parser.add_argument(
        "--residents",
        nargs="+",
        type=int,
        default=list(TARGET_RESIDENT_COUNTS),
        help="resident counts to exercise (default: 25 40)",
    )
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=8)
    return parser


async def _main() -> int:
    args = _parser().parse_args()
    reports = [
        await run_scale_scenario(
            count,
            rounds=args.rounds,
            concurrency=args.concurrency,
        )
        for count in args.residents
    ]
    print(json.dumps([report.to_dict() for report in reports], indent=2, sort_keys=True))
    return 0 if all(report.passed for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
