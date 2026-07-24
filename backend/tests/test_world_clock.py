"""Unit tests for the world clock — the single time-scale conversion entry point."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import world_clock as wc
from app.config import settings

SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def fixed_real(monkeypatch):
    """Pin real 'now' so world-time reads are deterministic. Returns a setter."""
    holder = {}

    def _set(dt: datetime):
        holder["now"] = dt
        monkeypatch.setattr(wc, "now_real", lambda: dt)

    return _set


def test_epoch_is_tz_aware_shanghai():
    epoch = wc.world_epoch()
    assert epoch.tzinfo is not None
    assert epoch.utcoffset() == timedelta(hours=8)
    assert (epoch.year, epoch.month, epoch.day) == (2026, 1, 1)
    assert (epoch.hour, epoch.minute) == (0, 0)


def test_both_clocks_equal_at_epoch():
    epoch = wc.world_epoch()
    # At the epoch instant, world time == real time.
    assert wc.real_to_world(epoch) == epoch
    assert wc.world_to_real(epoch) == epoch


def test_k_is_four_scaling():
    epoch = wc.world_epoch()
    # 1 real hour after epoch → k world hours after epoch.
    real = epoch + timedelta(hours=1)
    world = wc.real_to_world(real)
    assert world - epoch == timedelta(hours=settings.world_clock_k)


def test_real_to_world_world_to_real_roundtrip():
    epoch = wc.world_epoch()
    for offset_min in (0, 37, 6 * 60, 123456):
        real = epoch + timedelta(minutes=offset_min)
        back = wc.world_to_real(wc.real_to_world(real))
        assert abs((back - real).total_seconds()) < 1e-3


def test_now_world_matches_scaled_now_real(fixed_real):
    epoch = wc.world_epoch()
    fixed_real(epoch + timedelta(hours=2))  # 2 real hours in
    # 2 real hours → 8 world hours past midnight → world 08:00.
    assert wc.now_world() == epoch + timedelta(hours=8)
    assert wc.world_hour() == 8


def test_utc_plus_8_boundary(fixed_real):
    # A real instant given in UTC is coerced to +08:00 before scaling.
    epoch = wc.world_epoch()
    real_utc = (epoch + timedelta(hours=1)).astimezone(timezone.utc)
    fixed_real(real_utc)
    # Same physical instant → identical world time regardless of input tz.
    assert wc.now_world() == epoch + timedelta(hours=settings.world_clock_k)
    assert wc.now_world().tzinfo is not None


def test_crosses_world_day(fixed_real):
    epoch = wc.world_epoch()
    # 6 real hours = one full world day → world date advances by one day.
    fixed_real(epoch + timedelta(hours=6) - timedelta(minutes=1))
    before = wc.world_date_key()
    fixed_real(epoch + timedelta(hours=6) + timedelta(minutes=1))
    after = wc.world_date_key()
    assert before == "2026-01-01"
    assert after == "2026-01-02"


def test_world_weekday(fixed_real):
    epoch = wc.world_epoch()  # 2026-01-01 is a Thursday (weekday 3)
    assert epoch.weekday() == 3
    fixed_real(epoch)
    assert wc.world_weekday() == 3
    # +1 world day = +6 real hours → Friday (4).
    fixed_real(epoch + timedelta(hours=6))
    assert wc.world_weekday() == 4


def test_world_week_index(fixed_real):
    epoch = wc.world_epoch()
    fixed_real(epoch)
    assert wc.world_week_index() == 0
    # One world week = 7 world days = 7×6 = 42 real hours.
    fixed_real(epoch + timedelta(hours=42) - timedelta(minutes=1))
    assert wc.world_week_index() == 0
    fixed_real(epoch + timedelta(hours=42) + timedelta(minutes=1))
    assert wc.world_week_index() == 1


def test_next_beijing_morning_real(fixed_real):
    epoch = wc.world_epoch()
    # Real 2026-01-01 05:00 Beijing → next 07:00 is same day.
    fixed_real(epoch.replace(hour=5))
    nxt = wc.next_beijing_morning_real(7)
    assert (nxt.hour, nxt.minute) == (7, 0)
    assert nxt.date() == epoch.date()
    # Real 08:00 → past 07:00, so next is tomorrow 07:00.
    fixed_real(epoch.replace(hour=8))
    nxt2 = wc.next_beijing_morning_real(7)
    assert (nxt2.hour, nxt2.minute) == (7, 0)
    assert nxt2.date() == epoch.date() + timedelta(days=1)


def test_seconds_until_world_hour_scaled(fixed_real):
    epoch = wc.world_epoch()
    fixed_real(epoch)  # world 00:00
    # Next world 06:00 is 6 world hours away = 6/k real hours.
    secs = wc.seconds_until_world_hour(6)
    assert abs(secs - (6 / settings.world_clock_k) * 3600) < 1.0

