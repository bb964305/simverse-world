"""SBTI-driven daily schedule computation for resident autonomous behavior."""
import math
import random
from dataclasses import dataclass, field

from app.config import settings


@dataclass
class DailySchedule:
    """Computed schedule for a resident based on SBTI personality."""
    wake_hour: int           # Hour resident becomes active (0-23)
    sleep_hour: int          # Hour resident goes to sleep (0-23)
    peak_hours: list[int]    # Hours of maximum activity (1-3 values)
    social_slots: list[int]  # Hours with elevated social probability
    rest_ratio: float        # Fraction of awake time spent resting (0.0-1.0)


# SBTI dimension → schedule parameter mapping weights
_LEVEL = {"L": 0, "M": 1, "H": 2}


def _dim(sbti_data: dict, key: str) -> int:
    """Return numeric value (0=L, 1=M, 2=H) for a SBTI dimension."""
    dims = sbti_data.get("dimensions", {})
    return _LEVEL.get(dims.get(key, "M"), 1)


def _apply_weekend(sched: DailySchedule, weekday: int | None) -> DailySchedule:
    """Realism P1-9: on Sat/Sun sleep in (wake_hour+1), rest more (+0.1), and add
    one extra social slot. Off / weekday → unchanged."""
    if not settings.realism_enabled or weekday is None or weekday < 5:
        return sched
    new_wake = min(sched.wake_hour + settings.realism_weekend_wake_delay, sched.sleep_hour - 1)
    new_rest = min(1.0, sched.rest_ratio + settings.realism_weekend_rest_boost)
    slots = list(sched.social_slots)
    for cand in (new_wake + 6, new_wake + 8, new_wake + 4):
        if new_wake <= cand < sched.sleep_hour and cand not in slots:
            slots.append(cand)
            break
    return DailySchedule(
        wake_hour=new_wake, sleep_hour=sched.sleep_hour, peak_hours=sched.peak_hours,
        social_slots=sorted(slots), rest_ratio=round(new_rest, 3),
    )


def build_schedule(
    sbti_data: dict | None, weather: dict | None = None, weekday: int | None = None,
) -> DailySchedule:
    """Derive a DailySchedule from SBTI dimensions.

    Algorithm:
    - wake_hour: driven by Ac1 (motivation) + Ac3 (execution). High = early riser.
    - sleep_hour: driven by So1 (social) + E2 (emotional investment). High = stays up later.
    - peak_hours: 1-3 hours where resident is most active. Derived from Ac1 + A3.
    - social_slots: hours where social probability gets a +0.2 boost. From So1 + E2.
    - rest_ratio: driven by Ac3 inverted. Low Ac3 = high rest_ratio.

    E6: ``weather`` (the active weather event's payload, e.g. {"kind": "rain"})
    is accepted per the spec's API so the loop threads the current segment
    through, but it deliberately does NOT shift wake/sleep windows or tick
    probability — rain/storm influence behavior as a soft decide-prompt hint
    (see prompts.build_decision_prompt), keeping the clock deterministic.
    """
    del weather  # E6: no schedule effect by design — see docstring.
    if not sbti_data:
        return _apply_weekend(DailySchedule(
            wake_hour=8,
            sleep_hour=22,
            peak_hours=[10, 14],
            social_slots=[12, 19],
            rest_ratio=0.35,
        ), weekday)

    ac1 = _dim(sbti_data, "Ac1")  # motivation: 0-2
    ac3 = _dim(sbti_data, "Ac3")  # execution:  0-2
    so1 = _dim(sbti_data, "So1")  # social:     0-2
    e2  = _dim(sbti_data, "E2")   # emotional:  0-2
    a3  = _dim(sbti_data, "A3")   # meaning:    0-2

    # wake_hour: [5, 7, 9] for H, M, L motivation+execution
    drive = ac1 + ac3  # 0-4
    wake_hour = max(5, 9 - drive)

    # sleep_hour: [21, 22, 23] for L, M, H social+emotional
    # social_drive: 0-4; use (social_drive+1)//2 so M+M (=2) → +1 → 21, not 21
    # Mapping: 0→20, 1→21, 2→21, 3→22, 4→22 → use (social_drive+1)//2
    # Better: 0→21, 1-2→22, 3-4→23
    social_drive = so1 + e2  # 0-4
    if social_drive == 0:
        sleep_hour = 21
    elif social_drive <= 2:
        sleep_hour = 22
    else:
        sleep_hour = 23

    # peak_hours: 1-3 windows. High meaning → more peaks.
    base_peak = wake_hour + 2
    if a3 == 2:  # H meaning
        peak_hours = [base_peak, base_peak + 4, base_peak + 8]
    elif a3 == 1:  # M meaning
        peak_hours = [base_peak, base_peak + 5]
    else:  # L meaning — one late peak
        peak_hours = [base_peak + 3]
    # Clamp all peaks within awake window
    peak_hours = [h % 24 for h in peak_hours if wake_hour <= h % 24 < sleep_hour]
    if not peak_hours:
        peak_hours = [wake_hour + 2]

    # social_slots: So1=H → 3 slots, M → 2, L → 1
    social_base = wake_hour + 3
    if so1 == 2:
        social_slots = [social_base, social_base + 4, social_base + 7]
    elif so1 == 1:
        social_slots = [social_base, social_base + 6]
    else:
        social_slots = [social_base + 3]
    social_slots = [h % 24 for h in social_slots if wake_hour <= h % 24 < sleep_hour]

    # rest_ratio: Ac3=H → 0.2, M → 0.4, L → 0.6
    rest_ratio = 0.6 - (ac3 * 0.2)

    return _apply_weekend(DailySchedule(
        wake_hour=wake_hour,
        sleep_hour=sleep_hour,
        peak_hours=peak_hours,
        social_slots=social_slots,
        rest_ratio=rest_ratio,
    ), weekday)


def _weather_activity_factor(weather_kind: str | None) -> float:
    """Realism P1-8: weather multiplier on activity probability. Empty streets
    in a storm are a glance-level realism cue."""
    from app.config import settings
    return {
        "sunny": settings.realism_weather_sunny,
        "cloudy": settings.realism_weather_cloudy,
        "rain": settings.realism_weather_rain,
        "storm": settings.realism_weather_storm,
        "snow": settings.realism_weather_snow,
    }.get(weather_kind or "", 1.0)


def get_activity_probability(
    schedule: DailySchedule, hour: int, weather_kind: str | None = None,
    festival_active: bool = False,
) -> float:
    """Compute a 0.0-1.0 probability that a resident acts at this hour.

    Uses a smooth curve that:
    - Returns 0.0 outside [wake_hour, sleep_hour)
    - Peaks at peak_hours (up to 0.9)
    - Has a baseline of (1 - rest_ratio) * 0.5 during awake hours
    - Adds +0.2 boost at social_slots
    """
    # Outside awake window → no activity (unless debug override)
    from app.config import settings
    if getattr(settings, 'agent_debug_always_active', False):
        return 0.8  # Debug mode: always active
    if hour < schedule.wake_hour or hour >= schedule.sleep_hour:
        return 0.0

    baseline = (1.0 - schedule.rest_ratio) * 0.5

    # Peak boost: Gaussian around each peak hour
    peak_boost = 0.0
    for peak in schedule.peak_hours:
        distance = abs(hour - peak)
        # Gaussian with sigma=2 hours
        peak_boost = max(peak_boost, 0.4 * math.exp(-0.5 * (distance / 2.0) ** 2))

    # Social boost
    social_boost = 0.2 if hour in schedule.social_slots else 0.0
    # Realism P1-9: a festival lifts everyone's social-slot probability.
    if settings.realism_enabled and festival_active and hour in schedule.social_slots:
        social_boost += settings.realism_festival_social_boost

    prob = min(0.95, baseline + peak_boost + social_boost)
    # Realism P1-8: weather scales the whole activity probability (post-cap so a
    # storm can push the street below any baseline).
    if settings.realism_enabled and weather_kind:
        prob *= _weather_activity_factor(weather_kind)
    return prob


def should_tick(
    schedule: DailySchedule, hour: int, weather_kind: str | None = None,
    festival_active: bool = False,
) -> bool:
    """Roll against activity probability with ±15 minute jitter.

    The jitter means residents don't all wake up at exactly the same second,
    and slightly different residents will tick at different wall-clock moments.
    """
    prob = get_activity_probability(schedule, hour, weather_kind, festival_active)
    if prob <= 0.0:
        return False
    # Jitter: add small random noise to prob (±0.1)
    jittered = prob + random.uniform(-0.1, 0.1)
    return random.random() < jittered
