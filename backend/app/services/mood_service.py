"""E1 emotion engine — pure-rule mood updates (no LLM).

Mood is a {valence, arousal, label} dict on residents.mood_json. Events nudge
valence/arousal; a periodic decay regresses both toward neutral so a resident
returns to "calm" ~48h after the last event (5%/hour decay).
"""

import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.resident import Resident

logger = logging.getLogger(__name__)

NEUTRAL_VALENCE = 0.0
NEUTRAL_AROUSAL = 0.5
DECAY_RATE = 0.05  # fraction of the gap to neutral closed each decay pass


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def label_for(valence: float, arousal: float) -> str:
    """Map (valence, arousal) to one of 8 mood words. Neutral → calm."""
    if valence >= 0.15:
        if arousal >= 0.6:
            return "excited" if valence >= 0.5 else "content"
        return "content" if valence >= 0.5 else "calm"
    if valence <= -0.15:
        if arousal >= 0.6:
            return "furious" if valence <= -0.5 else "annoyed"
        return "gloomy" if valence <= -0.5 else "anxious"
    # near-neutral valence
    if arousal >= 0.75:
        return "excited"
    if arousal <= 0.3:
        return "tired"
    return "calm"


def default_mood() -> dict:
    return {
        "valence": NEUTRAL_VALENCE,
        "arousal": NEUTRAL_AROUSAL,
        "label": label_for(NEUTRAL_VALENCE, NEUTRAL_AROUSAL),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def get_mood(resident: Resident) -> dict:
    return resident.mood_json or default_mood()


def _apply(mood: dict, dv: float, da: float) -> dict:
    valence = _clamp(float(mood.get("valence", NEUTRAL_VALENCE)) + dv, -1.0, 1.0)
    arousal = _clamp(float(mood.get("arousal", NEUTRAL_AROUSAL)) + da, 0.0, 1.0)
    return {
        "valence": round(valence, 3),
        "arousal": round(arousal, 3),
        "label": label_for(valence, arousal),
        "updated_at": datetime.now(UTC).isoformat(),
    }


async def apply_mood_event(db: AsyncSession, resident: Resident, dv: float, da: float = 0.0) -> dict:
    """Nudge a resident's mood by (dv, da) and persist. Returns the new mood."""
    resident.mood_json = _apply(get_mood(resident), dv, da)
    await db.commit()
    return resident.mood_json


async def apply_mood_event_by_id(db: AsyncSession, resident_id: str, dv: float, da: float = 0.0) -> dict | None:
    resident = await db.get(Resident, resident_id)
    if resident is None:
        return None
    return await apply_mood_event(db, resident, dv, da)


async def decay_all(db: AsyncSession) -> int:
    """Regress every set mood toward neutral by DECAY_RATE. Returns count touched.

    Mood lives inside a JSON column, so a portable single-statement UPDATE can't
    touch it — we batch-load residents with a mood and update them. (Deviation
    from the spec's single-SQL note, which isn't portable for JSON.)
    """
    residents = (await db.execute(
        select(Resident).where(Resident.mood_json.is_not(None))
    )).scalars().all()
    n = 0
    for r in residents:
        mood = r.mood_json or {}
        v = float(mood.get("valence", NEUTRAL_VALENCE))
        a = float(mood.get("arousal", NEUTRAL_AROUSAL))
        nv = v + (NEUTRAL_VALENCE - v) * DECAY_RATE
        na = a + (NEUTRAL_AROUSAL - a) * DECAY_RATE
        r.mood_json = {
            "valence": round(nv, 3),
            "arousal": round(na, 3),
            "label": label_for(nv, na),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        n += 1
    if n:
        await db.commit()
    return n


async def apply_weather_mood(db: AsyncSession, *, hour: int | None = None) -> int:
    """Realism P1-8: a weak, long-running weather nudge on mood (hourly via
    heat_cron). rain/storm depress; a sunny morning lifts a little. Off = no-op."""
    from app.config import settings
    if not settings.realism_enabled:
        return 0
    from app.tasks.weather import get_current_weather
    kind = (await get_current_weather(db) or {}).get("kind")
    if hour is None:
        hour = datetime.now().hour
    if kind in ("rain", "storm"):
        dv = settings.realism_weather_mood_rain_valence
        da = settings.realism_weather_mood_rain_arousal
    elif kind == "sunny" and 6 <= hour < 10:
        dv = settings.realism_weather_mood_sunny_valence
        da = 0.0
    else:
        return 0
    residents = (await db.execute(
        select(Resident).where(Resident.status != "sleeping")
    )).scalars().all()
    for r in residents:
        r.mood_json = _apply(get_mood(r), dv, da)
    if residents:
        await db.commit()
    return len(residents)
