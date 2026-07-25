"""World clock — the single time-scale conversion entry point (agent-T).

Two kinds of time live in this world (see docs/ROADMAP.md):

- **World time** — an accelerated clock the *simulation* runs on. It equals
  ``WORLD_EPOCH + k × (real elapsed since WORLD_EPOCH)`` with ``k = WORLD_CLOCK_K``
  (default 4: one real day = four world days, a full day/night every 6 real
  hours). Everything about resident life that has a "time of day / weekday /
  calendar date" meaning — 作息 (wake/sleep), 星期节律, 日报叙事, 计划日期 — reads
  world time.
- **Real time** — ordinary wall-clock time, expressed in the world's anchor zone
  ``Asia/Shanghai`` (UTC+8, no DST). Operational concerns keep using it: the LLM
  budget day-close, nightly cron cadence, Redis TTL / rate-limit / cooldown,
  log timestamps, TLS.

All world-time reads MUST go through this module; no other code should call
``datetime.now()`` to derive world time. Every parameter and return value is
tz-aware (Asia/Shanghai). Config is read lazily inside each function so tests can
monkeypatch ``settings`` / this module's seams without re-import.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from app.config import settings


def _zone() -> tzinfo:
    """The anchor timezone (Asia/Shanghai). Falls back to a fixed +08:00 offset
    if the system lacks the IANA tz database — China has no DST, so the two are
    equivalent for this world's purposes."""
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # pragma: no cover — only when tzdata is unavailable
        return timezone(timedelta(hours=8))


def _k() -> int:
    return settings.world_clock_k


def world_epoch() -> datetime:
    """The fixed instant where world time == real time (both clocks agree).

    Parsed from ``settings.world_epoch`` (ISO-8601, tz-aware) and normalized into
    the anchor zone so date/hour arithmetic is consistent everywhere."""
    dt = datetime.fromisoformat(settings.world_epoch)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone())
    return dt.astimezone(_zone())


def _as_zone(dt: datetime) -> datetime:
    """Coerce an input datetime into the anchor zone. A naive datetime is assumed
    to be UTC (that is how the DB stores ``created_at``), then converted."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_zone())


def now_real() -> datetime:
    """Current real time, expressed in the anchor zone (Asia/Shanghai)."""
    return datetime.now(_zone())


def real_to_world(dt: datetime) -> datetime:
    """Map a real instant to its world instant: ``EPOCH + k×(dt − EPOCH)``."""
    dt = _as_zone(dt)
    epoch = world_epoch()
    return epoch + _k() * (dt - epoch)


def world_to_real(dt: datetime) -> datetime:
    """Inverse of :func:`real_to_world`: ``EPOCH + (dt − EPOCH)/k``."""
    dt = _as_zone(dt)
    epoch = world_epoch()
    return epoch + (dt - epoch) / _k()


def now_world() -> datetime:
    """Current world time (tz-aware Asia/Shanghai). The one true 'now' for the sim."""
    return real_to_world(now_real())


def world_hour() -> int:
    """World hour of day 0-23 — feeds the resident activity scheduler."""
    return now_world().hour


def world_weekday() -> int:
    """World weekday, Mon=0 .. Sun=6 — feeds weekend/market/festival logic."""
    return now_world().weekday()


def world_date_key() -> str:
    """World calendar date as ``YYYY-MM-DD`` — the plan/dedup/day key."""
    return now_world().strftime("%Y-%m-%d")


def world_week_index() -> int:
    """Zero-based world-week ordinal since ``WORLD_EPOCH``.

    A world week is 7 world days. Because a world week is only 1.75 real days,
    weekly gates cannot use ``weekday()==k`` equality (they would misfire); the
    nightly cron instead stores this ordinal and fires when it advances."""
    delta = now_world() - world_epoch()
    return int(delta // timedelta(weeks=1))


def next_beijing_morning_real(hour: int) -> datetime:
    """The next real instant at ``hour``:00 Beijing time (anchor for nightly cron).

    Returns a real (not world) datetime so the cron keeps its true-24h cadence
    while landing the digest in the Beijing morning."""
    now = now_real()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def seconds_until_world_hour(h: int) -> float:
    """Real seconds until the next world-time ``h``:00 (spare helper).

    World hours pass k× faster than real, so a world hour of runway is only
    ``1/k`` real hours away."""
    now_w = now_world()
    target_w = now_w.replace(hour=h, minute=0, second=0, microsecond=0)
    if target_w <= now_w:
        target_w += timedelta(days=1)
    return (world_to_real(target_w) - now_real()).total_seconds()
