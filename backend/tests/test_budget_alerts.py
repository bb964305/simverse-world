"""Roadmap #6: the budget circuit breaker must not fail SILENTLY.

Two alert signals (app/llm/budget_alerts.py):
1. meter-read failure — the spend SUM() in app.llm.budget raised; the breaker
   fails open to NORMAL (unchanged), but now emits WARN + Sentry event.
2. usage stall — AGENT_ENABLED=true, metering on, agent loop observed running,
   yet llm_usage has had zero new rows for >= N minutes: the breaker may be
   blind while the world keeps spending.

Thresholds are plain environment variables (NOT config.py fields — see task C
red line): BUDGET_ALERTS_ENABLED / BUDGET_ALERT_COOLDOWN_MIN /
BUDGET_USAGE_STALL_MIN. Defaults are conservative; BUDGET_ALERTS_ENABLED=false
is the one-switch off.
"""
import logging
import time
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.database import Base
from app.models.llm_usage import LLMUsage
from app.llm import budget_alerts
from app.llm.budget import BudgetTier, background_tier, user_over_budget

pytestmark = pytest.mark.anyio

ALERT_LOGGER = "app.llm.budget_alerts"


class _BrokenSession:
    """Session whose spend query always raises (metering DB down)."""

    async def execute(self, *args, **kwargs):
        raise RuntimeError("boom: metering db unreachable")


@pytest.fixture(autouse=True)
def _clean_alert_state(monkeypatch):
    """Fresh module state + no alert env leakage between tests."""
    for var in ("BUDGET_ALERTS_ENABLED", "BUDGET_ALERT_COOLDOWN_MIN",
                "BUDGET_USAGE_STALL_MIN"):
        monkeypatch.delenv(var, raising=False)
    budget_alerts.reset_state_for_tests()
    yield
    budget_alerts.reset_state_for_tests()


@pytest.fixture
def sentry_events(monkeypatch):
    """Record would-be Sentry events without importing sentry_sdk."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        budget_alerts, "_sentry_event",
        lambda message, extra=None: sent.append((message, dict(extra or {}))),
    )
    return sent


@pytest.fixture
def metering_on(monkeypatch):
    monkeypatch.setattr(settings, "llm_metering_enabled", True)
    monkeypatch.setattr(settings, "budget_global_daily_usd", 1.0)
    monkeypatch.setattr(settings, "budget_user_daily_usd", 1.0)
    monkeypatch.setattr(settings, "agent_enabled", True)


@pytest.fixture
async def usage_session():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Signal 1: meter-read failure
# ---------------------------------------------------------------------------

async def test_meter_read_failure_warns_and_sends_sentry(
    caplog, sentry_events, metering_on
):
    with caplog.at_level(logging.WARNING, logger=ALERT_LOGGER):
        tier = await background_tier(_BrokenSession())
    assert tier == BudgetTier.NORMAL  # fail-open behavior unchanged
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns and "background_tier" in warns[0].getMessage()
    assert len(sentry_events) == 1
    assert "background_tier" in sentry_events[0][0]


async def test_user_over_budget_failure_also_alerts(
    caplog, sentry_events, metering_on
):
    with caplog.at_level(logging.WARNING, logger=ALERT_LOGGER):
        over = await user_over_budget(_BrokenSession(), "u1")
    assert over is False  # fail-open behavior unchanged
    assert len(sentry_events) == 1
    assert "user_over_budget" in sentry_events[0][0]


async def test_kill_switch_disables_all_alerts(
    caplog, sentry_events, metering_on, monkeypatch
):
    monkeypatch.setenv("BUDGET_ALERTS_ENABLED", "false")
    with caplog.at_level(logging.WARNING, logger=ALERT_LOGGER):
        tier = await background_tier(_BrokenSession())
    assert tier == BudgetTier.NORMAL
    assert not [r for r in caplog.records if r.name == ALERT_LOGGER]
    assert sentry_events == []


async def test_cooldown_suppresses_repeat_alerts(
    caplog, sentry_events, metering_on, monkeypatch
):
    with caplog.at_level(logging.WARNING, logger=ALERT_LOGGER):
        await background_tier(_BrokenSession())
        await background_tier(_BrokenSession())  # within default 30min cooldown
    assert len(sentry_events) == 1
    assert len([r for r in caplog.records if r.name == ALERT_LOGGER]) == 1

    # cooldown 0 → every failure alerts
    monkeypatch.setenv("BUDGET_ALERT_COOLDOWN_MIN", "0")
    with caplog.at_level(logging.WARNING, logger=ALERT_LOGGER):
        await background_tier(_BrokenSession())
        await background_tier(_BrokenSession())
    assert len(sentry_events) == 3


# ---------------------------------------------------------------------------
# Signal 2: llm_usage stall watchdog
# ---------------------------------------------------------------------------

def _arm_loop_observation(minutes_ago: float) -> None:
    budget_alerts._loop_first_seen = time.monotonic() - minutes_ago * 60


async def test_stall_alerts_when_no_new_usage_rows(
    usage_session, caplog, sentry_events, metering_on, monkeypatch
):
    monkeypatch.setenv("BUDGET_USAGE_STALL_MIN", "1")
    usage_session.add(LLMUsage(
        scenario="decide", model="m", owner="system", cost_usd=0.01,
        ts=datetime.now(UTC) - timedelta(hours=2),
    ))
    await usage_session.commit()
    _arm_loop_observation(minutes_ago=5)

    with caplog.at_level(logging.WARNING, logger=ALERT_LOGGER):
        stalled = await budget_alerts.check_usage_stall(usage_session)
    assert stalled is True
    assert len(sentry_events) == 1
    assert "llm_usage" in sentry_events[0][0]


async def test_no_stall_when_recent_usage_row(
    usage_session, sentry_events, metering_on, monkeypatch
):
    monkeypatch.setenv("BUDGET_USAGE_STALL_MIN", "1")
    usage_session.add(LLMUsage(
        scenario="decide", model="m", owner="system", cost_usd=0.01,
        ts=datetime.now(UTC) - timedelta(seconds=10),
    ))
    await usage_session.commit()
    _arm_loop_observation(minutes_ago=5)

    assert await budget_alerts.check_usage_stall(usage_session) is False
    assert sentry_events == []


async def test_stall_on_empty_table_after_observation_window(
    usage_session, sentry_events, metering_on, monkeypatch
):
    monkeypatch.setenv("BUDGET_USAGE_STALL_MIN", "1")
    _arm_loop_observation(minutes_ago=5)
    assert await budget_alerts.check_usage_stall(usage_session) is True
    assert len(sentry_events) == 1


async def test_stall_needs_observation_window_first(
    usage_session, sentry_events, metering_on, monkeypatch
):
    """Right after startup the loop hasn't been watched N minutes — no verdict."""
    monkeypatch.setenv("BUDGET_USAGE_STALL_MIN", "1")
    assert await budget_alerts.check_usage_stall(usage_session) is False
    assert sentry_events == []


async def test_stall_disabled_by_zero_threshold(
    usage_session, sentry_events, metering_on, monkeypatch
):
    monkeypatch.setenv("BUDGET_USAGE_STALL_MIN", "0")
    _arm_loop_observation(minutes_ago=999)
    assert await budget_alerts.check_usage_stall(usage_session) is False
    assert sentry_events == []


async def test_stall_requires_agent_and_metering_enabled(
    usage_session, sentry_events, metering_on, monkeypatch
):
    monkeypatch.setenv("BUDGET_USAGE_STALL_MIN", "1")
    _arm_loop_observation(minutes_ago=5)
    monkeypatch.setattr(settings, "agent_enabled", False)
    assert await budget_alerts.check_usage_stall(usage_session) is False
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(settings, "llm_metering_enabled", False)
    assert await budget_alerts.check_usage_stall(usage_session) is False
    assert sentry_events == []


async def test_stall_default_threshold_is_conservative_24h(
    usage_session, sentry_events, metering_on
):
    """Default 1440min: a 2h-quiet world (daily-cap dormancy) must NOT alert."""
    usage_session.add(LLMUsage(
        scenario="decide", model="m", owner="system", cost_usd=0.01,
        ts=datetime.now(UTC) - timedelta(hours=2),
    ))
    await usage_session.commit()
    _arm_loop_observation(minutes_ago=10_000)  # loop long-observed
    assert await budget_alerts.check_usage_stall(usage_session) is False
    assert sentry_events == []


# ---------------------------------------------------------------------------
# Loop-facing wrapper: opens its own session only when armed
# ---------------------------------------------------------------------------

async def test_maybe_check_uses_injected_factory_and_min_interval(
    sentry_events, metering_on, monkeypatch
):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setenv("BUDGET_USAGE_STALL_MIN", "1")
    _arm_loop_observation(minutes_ago=5)
    try:
        assert await budget_alerts.maybe_check_usage_stall(session_factory=factory) is True
        assert len(sentry_events) == 1
        # min query interval: an immediate second call does not re-query/alert
        assert await budget_alerts.maybe_check_usage_stall(session_factory=factory) is False
        assert len(sentry_events) == 1
    finally:
        await engine.dispose()


async def test_maybe_check_noop_when_disarmed(metering_on, monkeypatch, sentry_events):
    monkeypatch.setenv("BUDGET_USAGE_STALL_MIN", "0")

    def _explode():  # factory must never be touched when disarmed
        raise AssertionError("session factory must not be used")

    assert await budget_alerts.maybe_check_usage_stall(session_factory=_explode) is False
    assert sentry_events == []


# ---------------------------------------------------------------------------
# Sentry plumbing stays inert without a DSN
# ---------------------------------------------------------------------------

def test_sentry_event_noop_without_dsn(monkeypatch):
    monkeypatch.setattr(settings, "sentry_dsn", "", raising=False)
    budget_alerts._sentry_event("msg", extra={"k": "v"})  # must not raise
