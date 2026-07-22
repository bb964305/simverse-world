"""Realism P1-9: weekend schedule shift + festival social boost."""
import pytest

from app.config import settings
from app.agent.scheduler import build_schedule, get_activity_probability


def test_weekend_shifts_schedule(monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    weekday = build_schedule(None, weekday=2)   # Wednesday
    weekend = build_schedule(None, weekday=5)   # Saturday
    assert weekend.wake_hour == weekday.wake_hour + settings.realism_weekend_wake_delay
    assert weekend.rest_ratio > weekday.rest_ratio
    assert len(weekend.social_slots) == len(weekday.social_slots) + 1


def test_weekday_ignored_when_realism_off(monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", False)
    weekday = build_schedule(None, weekday=2)
    weekend = build_schedule(None, weekday=5)
    assert weekend.wake_hour == weekday.wake_hour
    assert weekend.rest_ratio == weekday.rest_ratio


def test_festival_boosts_social_slot_probability(monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    sched = build_schedule(None)  # social_slots [12, 19]
    slot_hour = sched.social_slots[0]
    base = get_activity_probability(sched, slot_hour, None, festival_active=False)
    fest = get_activity_probability(sched, slot_hour, None, festival_active=True)
    assert fest > base
    # non-social hour: festival has no effect
    non_social = next(h for h in range(sched.wake_hour, sched.sleep_hour)
                      if h not in sched.social_slots)
    assert (get_activity_probability(sched, non_social, None, festival_active=True)
            == get_activity_probability(sched, non_social, None, festival_active=False))
