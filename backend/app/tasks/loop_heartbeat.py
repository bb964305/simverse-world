"""Background-loop heartbeats + death alerting (P2, Roadmap #5).

Why this exists
---------------
The loops started in ``app/main.py`` (heat / event / nightly / agent /
embedding_backfill / caravan) run in exactly one process — the API worker with
``run_background_tasks=true``, or the standalone agent-worker in split mode.
If one of them dies (task raised out of its ``while True``, task cancelled,
worker started without them) the world silently stops doing that job: no log,
no metric, no alert. Multi-instance deployments make it worse — nothing tells
you *which* instance was supposed to own the loops.

Each loop now stamps ``sv:hb:<loop>`` with an ISO timestamp once per round.
Two consumers:

- ``GET /health/loops`` — read-only view, works from any process because the
  heartbeats live in Redis rather than in the owning process's memory.
- ``check_stale`` — WARN log + Sentry event when a heartbeat is older than
  its threshold, cooldown-limited so a dead loop cannot flood. It is driven
  from ``beat`` itself (throttled), so any surviving loop reports on its dead
  siblings. Modelled on ``app.llm.budget_alerts`` (commit a3a32ec).

A loop that has *never* beaten is reported as ``never_seen`` and never alerts:
a deployment that deliberately runs without background tasks is a config
choice, not an outage.

Configuration is plain **environment variables**, deliberately NOT
``app.config.Settings`` fields (batch red line: config.py is being edited by
two other parallel lines; the keys get registered in .env.example at
integration time):

    LOOP_HEARTBEAT_ENABLED            default "true"  — one-switch off
    LOOP_HEARTBEAT_STALE_FACTOR       default 3       — stale after N x the
                                                        loop's own interval
    LOOP_HEARTBEAT_MIN_STALE_SEC      default 900     — floor, so the 60s event
                                                        cron cannot page on a
                                                        single slow round
    LOOP_HEARTBEAT_ALERT_COOLDOWN_MIN default 60      — min minutes between
                                                        identical alerts
    LOOP_HEARTBEAT_CHECK_INTERVAL_MIN default 5       — how often a beat may
                                                        trigger a sweep
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, UTC

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}
_PREFIX = "sv:hb:"

# Nominal seconds between rounds for each loop. Agent, embedding and caravan
# cadence are settings, so they are resolved lazily in ``_interval_s``.
LOOP_INTERVALS: dict[str, float | None] = {
    "heat": 3600.0,                # heat_cron.HEAT_CRON_INTERVAL_SECONDS
    "event": 60.0,                 # event_cron.EVENT_CRON_INTERVAL_SECONDS
    "nightly": 86400.0,            # one Beijing-morning anchor per real day
    "agent": None,                 # settings.agent_tick_interval
    "embedding_backfill": None,    # settings.embedding_backfill_interval_seconds
    "caravan": None,               # settings.caravan_lifecycle_interval_seconds
}

# Heartbeats outlive any plausible threshold but are not kept forever, so a
# retired loop's key disappears on its own.
_HEARTBEAT_TTL_S = 7 * 86400

# Module state (background loops are single-process; no locking needed).
_last_alert: dict[str, float] = {}
_last_check_at: float | None = None


def reset_state_for_tests() -> None:
    global _last_check_at
    _last_alert.clear()
    _last_check_at = None


# --------------------------------------------------------------------------- #
# config helpers                                                               #
# --------------------------------------------------------------------------- #

def _settings_default(name: str, default):
    """Registered default for an env knob (收口 2026-07-25B).

    The env var stays the runtime source (ops can retune without a restart);
    ``Settings`` merely owns the fallback value so every .env.example key maps
    to a real field. A missing field keeps the caller's literal default.
    """
    try:
        from app.config import settings

        return getattr(settings, name.lower(), default)
    except Exception:  # config import must never break a heartbeat
        return default


def _env_float(name: str, default: float) -> float:
    default = float(_settings_default(name, default))
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("invalid %s=%r — using default %s", name, raw, default)
        return default


def heartbeats_enabled() -> bool:
    raw = os.environ.get("LOOP_HEARTBEAT_ENABLED")
    if raw is None or raw.strip() == "":
        return bool(_settings_default("LOOP_HEARTBEAT_ENABLED", True))
    return raw.strip().lower() in _TRUE


def heartbeat_key(name: str) -> str:
    return _PREFIX + name


def _interval_s(name: str) -> float:
    interval = LOOP_INTERVALS.get(name)
    if interval is not None:
        return interval
    try:
        from app.config import settings

        if name == "embedding_backfill":
            return float(settings.embedding_backfill_interval_seconds)
        if name == "caravan":
            return float(settings.caravan_lifecycle_interval_seconds)
        return float(settings.agent_tick_interval)
    except Exception:
        return 60.0


def stale_threshold_s(name: str) -> float:
    """Age at which ``name``'s heartbeat counts as dead."""
    factor = _env_float("LOOP_HEARTBEAT_STALE_FACTOR", 3.0) or 3.0
    floor = _env_float("LOOP_HEARTBEAT_MIN_STALE_SEC", 900.0)
    return max(_interval_s(name) * factor, floor)


def _cooldown_s() -> float:
    return _env_float("LOOP_HEARTBEAT_ALERT_COOLDOWN_MIN", 60.0) * 60


def _check_interval_s() -> float:
    return _env_float("LOOP_HEARTBEAT_CHECK_INTERVAL_MIN", 5.0) * 60


def _sentry_event(message: str, extra: dict | None = None) -> None:
    """One warning-level event; inert without a DSN, never raises."""
    try:
        from app.config import settings

        if not settings.sentry_dsn:
            return
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            scope.set_tag("alert", "loop_heartbeat")
            for k, v in (extra or {}).items():
                scope.set_extra(k, str(v))
            sentry_sdk.capture_message(message, level="warning")
    except Exception:  # alerting must never break the caller
        logger.debug("sentry heartbeat alert failed", exc_info=True)


# --------------------------------------------------------------------------- #
# writing                                                                      #
# --------------------------------------------------------------------------- #

async def beat(name: str, *, check: bool = True) -> None:
    """Record one round of ``name``. Never raises — a loop must not die of this.

    ``check`` also runs the stale sweep, throttled to one sweep per
    ``LOOP_HEARTBEAT_CHECK_INTERVAL_MIN``, so any living loop reports on its
    dead siblings without a watchdog process of its own.
    """
    global _last_check_at
    try:
        if not heartbeats_enabled():
            return
        await get_redis().set(
            heartbeat_key(name), datetime.now(UTC).isoformat(), ex=_HEARTBEAT_TTL_S
        )
    except Exception:
        logger.debug("heartbeat write failed for %s", name, exc_info=True)
        return
    if not check:
        return
    try:
        now_mono = time.monotonic()
        if _last_check_at is not None and (now_mono - _last_check_at) < _check_interval_s():
            return
        _last_check_at = now_mono
        await check_stale()
    except Exception:
        logger.debug("heartbeat sweep failed", exc_info=True)


async def clear_owned_heartbeats(names: tuple[str, ...] | list[str]) -> None:
    """Forget stale leases before a new loop-owner process starts.

    Docker health checks must prove that the *new* worker emitted a beat.  The
    Redis keys otherwise survive a container replacement for up to seven days,
    allowing a broken replacement to borrow the previous worker's fresh state.
    This operation is fail-open for process startup; a Redis outage is handled
    by the health check itself, which cannot report ``ok`` without Redis.
    """
    keys = [heartbeat_key(name) for name in names if name in LOOP_INTERVALS]
    if not keys:
        return
    try:
        await get_redis().delete(*keys)
    except Exception:
        logger.warning("failed to clear prior worker heartbeats", exc_info=True)


# --------------------------------------------------------------------------- #
# reading                                                                      #
# --------------------------------------------------------------------------- #

async def snapshot(now: datetime | None = None) -> dict[str, dict]:
    """Per-loop {state, age_seconds, threshold_seconds, last_beat} view.

    ``state`` is one of ``ok`` / ``stale`` / ``never_seen``. Read-only; a Redis
    failure yields ``never_seen`` for everything rather than raising.
    """
    now = now or datetime.now(UTC)
    try:
        r = get_redis()
        raws = {name: await r.get(heartbeat_key(name)) for name in LOOP_INTERVALS}
    except Exception:
        logger.debug("heartbeat read failed", exc_info=True)
        raws = {name: None for name in LOOP_INTERVALS}

    out: dict[str, dict] = {}
    for name in LOOP_INTERVALS:
        threshold = stale_threshold_s(name)
        last = _parse(raws.get(name))
        if last is None:
            out[name] = {
                "state": "never_seen", "age_seconds": None,
                "threshold_seconds": threshold, "last_beat": None,
            }
            continue
        age = (now - last).total_seconds()
        out[name] = {
            "state": "stale" if age >= threshold else "ok",
            "age_seconds": round(age, 1),
            "threshold_seconds": threshold,
            "last_beat": last.isoformat(),
        }
    return out


def _parse(raw) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# alerting                                                                     #
# --------------------------------------------------------------------------- #

async def check_stale() -> list[str]:
    """WARN + Sentry for every loop whose heartbeat expired. Never raises.

    Returns the loops alerted on *this* call (cooldown-suppressed ones are not
    listed, so a caller can log "newly dead" without re-reporting).
    """
    if not heartbeats_enabled():
        return []
    try:
        snap = await snapshot()
    except Exception:
        logger.debug("heartbeat snapshot failed", exc_info=True)
        return []

    alerted: list[str] = []
    now_mono = time.monotonic()
    for name, info in snap.items():
        if info["state"] != "stale":
            if info["state"] == "ok":
                _last_alert.pop(name, None)  # recovered -> re-arm
            continue
        last = _last_alert.get(name)
        if last is not None and (now_mono - last) < _cooldown_s():
            continue
        _last_alert[name] = now_mono
        logger.warning(
            "background loop %r has not beaten for %.0fs (threshold %.0fs) — it is "
            "probably dead; its jobs are silently not running (Roadmap #5). "
            "last_beat=%s",
            name, info["age_seconds"], info["threshold_seconds"], info["last_beat"],
        )
        _sentry_event(
            f"background loop {name} heartbeat expired — loop probably dead",
            extra={
                "loop": name,
                "age_seconds": info["age_seconds"],
                "threshold_seconds": info["threshold_seconds"],
                "last_beat": info["last_beat"],
            },
        )
        alerted.append(name)
    return alerted
