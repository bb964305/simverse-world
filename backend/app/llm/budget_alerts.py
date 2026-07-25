"""Budget circuit-breaker silent-failure alerts (Roadmap #6).

The breaker in ``app.llm.budget`` fails open **by design** — a metering
hiccup must never freeze the world. The flip side: if the spend query breaks
permanently (metering DB gone, schema drift), the breaker silently reports
NORMAL forever and cost control is gone with no signal. This module adds the
signal without touching the fail-open behavior:

1. **meter-read failure** — a spend ``SUM()`` in ``app.llm.budget`` raised:
   WARN log + Sentry event (cooldown-limited so a dead DB doesn't flood).
2. **usage stall** — AGENT_ENABLED=true, metering on, the agent loop has been
   observed running for >= N minutes, yet ``llm_usage`` got **zero new rows**
   in the last N minutes. Either metering writes are broken (breaker blind
   while spend continues) or the world is genuinely frozen — both warrant a
   look. Checked once per round from ``AgentLoop.run`` via
   ``maybe_check_usage_stall``.

Configuration is plain **environment variables**, deliberately NOT
``app.config.Settings`` fields (task C red line: config.py untouched; the
keys get registered in .env.example at integration time — note that
test_env_example_consistency invariant 1 will then need these mirrored into
Settings or whitelisted):

    BUDGET_ALERTS_ENABLED      default "true"   — one-switch off ("false")
    BUDGET_ALERT_COOLDOWN_MIN  default "30"     — min minutes between
                                                  identical alerts (log+Sentry)
    BUDGET_USAGE_STALL_MIN     default "1440"   — stall window N minutes;
                                                  "0" disables the watchdog.

The stall default is deliberately 24h-conservative: daily-cap dormancy
legitimately silences the world for ~21h (see burn-in notes), so any window
shorter than a day would page on healthy nights. Sentry is imported lazily
and only used when SENTRY_DSN is set (same convention as app.observability).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, UTC

from app.config import settings

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}

# Module state (single-process background loop; no locking needed).
_last_sent: dict[str, float] = {}      # alert key -> monotonic seconds
_loop_first_seen: float | None = None  # first stall check while armed
_last_query_at: float | None = None    # maybe_check throttle

# maybe_check_usage_stall never queries more often than this, regardless of
# tick interval (the max(ts) probe is cheap, but there is no reason to run it
# every few seconds).
_MIN_QUERY_INTERVAL_S = 60.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _env_minutes(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("invalid %s=%r — using default %s", name, raw, default)
        return default


def alerts_enabled() -> bool:
    return _env_bool("BUDGET_ALERTS_ENABLED", True)


def _cooldown_s() -> float:
    return _env_minutes("BUDGET_ALERT_COOLDOWN_MIN", 30.0) * 60


def _stall_threshold_s() -> float:
    return _env_minutes("BUDGET_USAGE_STALL_MIN", 1440.0) * 60


def reset_state_for_tests() -> None:
    global _loop_first_seen, _last_query_at
    _last_sent.clear()
    _loop_first_seen = None
    _last_query_at = None


def _should_send(key: str) -> bool:
    """True at most once per cooldown window per alert key."""
    now = time.monotonic()
    last = _last_sent.get(key)
    if last is not None and (now - last) < _cooldown_s():
        return False
    _last_sent[key] = now
    return True


def _sentry_event(message: str, extra: dict | None = None) -> None:
    """Send one warning-level event; inert without a DSN, never raises."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            scope.set_tag("alert", "budget_breaker")
            for k, v in (extra or {}).items():
                scope.set_extra(k, str(v))
            sentry_sdk.capture_message(message, level="warning")
    except Exception:  # alerting must never break the caller
        logger.debug("sentry budget alert failed", exc_info=True)


# ---------------------------------------------------------------------------
# Signal 1 — called from app.llm.budget except-blocks
# ---------------------------------------------------------------------------

def alert_meter_read_failure(where: str, exc: Exception) -> None:
    """A spend query raised and the breaker failed open. Never raises."""
    try:
        if not alerts_enabled():
            return
        if not _should_send(f"meter_read:{where}"):
            return
        logger.warning(
            "budget breaker meter read FAILED in %s — failing open to NORMAL, "
            "cost control may be blind (Roadmap #6): %s", where, exc,
        )
        _sentry_event(
            f"budget breaker meter read failed in {where}; failing open",
            extra={"where": where, "error": repr(exc)},
        )
    except Exception:
        logger.debug("alert_meter_read_failure itself failed", exc_info=True)


# ---------------------------------------------------------------------------
# Signal 2 — llm_usage stall watchdog
# ---------------------------------------------------------------------------

def _stall_armed() -> bool:
    return (
        alerts_enabled()
        and _stall_threshold_s() > 0
        and settings.agent_enabled
        and settings.llm_metering_enabled
    )


async def check_usage_stall(session) -> bool:
    """Alert when llm_usage got no new row for >= N minutes while the loop runs.

    Returns True when a stall alert was emitted this call. The observation
    window starts at the first armed call (loop startup), so a fresh boot
    never alerts before the loop has actually been watched for N minutes.
    """
    global _loop_first_seen
    if not _stall_armed():
        _loop_first_seen = None  # re-arm cleanly after a config flip
        return False
    threshold = _stall_threshold_s()
    now_mono = time.monotonic()
    if _loop_first_seen is None:
        _loop_first_seen = now_mono
    if now_mono - _loop_first_seen < threshold:
        return False  # not observed long enough for a verdict

    from sqlalchemy import func, select

    from app.models.llm_usage import LLMUsage

    try:
        latest = (await session.execute(select(func.max(LLMUsage.ts)))).scalar()
    except Exception as e:
        alert_meter_read_failure("usage_stall_check", e)
        return False

    if latest is not None:
        if latest.tzinfo is None:  # sqlite/legacy rows are naive-UTC
            latest = latest.replace(tzinfo=UTC)
        if (datetime.now(UTC) - latest).total_seconds() < threshold:
            return False

    if not _should_send("usage_stall"):
        return False
    minutes = threshold / 60
    logger.warning(
        "llm_usage has ZERO new rows for >= %.0f min while AGENT_ENABLED=true "
        "and metering is on — budget breaker may be silently blind, or the "
        "world is frozen (Roadmap #6). latest_ts=%s", minutes, latest,
    )
    _sentry_event(
        f"llm_usage stalled: no new rows for >= {minutes:.0f} min with agent "
        "loop running — budget metering may be silently broken",
        extra={"threshold_min": minutes, "latest_ts": latest},
    )
    return True


async def maybe_check_usage_stall(session_factory=None) -> bool:
    """Loop-facing wrapper: cheap no-op unless armed; own short session.

    Called once per AgentLoop round; throttled to one DB probe per
    ``_MIN_QUERY_INTERVAL_S`` regardless of tick interval. Never raises.
    """
    global _last_query_at
    try:
        if not _stall_armed():
            return False
        now_mono = time.monotonic()
        if _last_query_at is not None and (now_mono - _last_query_at) < _MIN_QUERY_INTERVAL_S:
            return False
        _last_query_at = now_mono
        if session_factory is None:
            from app.database import async_session as session_factory
        async with session_factory() as session:
            return await check_usage_stall(session)
    except Exception:
        logger.debug("usage stall check failed", exc_info=True)
        return False
