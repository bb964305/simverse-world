"""E6 weather machine: pure-rule Markov weather segments riding the S1 event bus.

Every event-cron pass calls ``ensure_weather_event``: when no weather
world-event is still running (or scheduled), it samples the next segment from a
seasonal transition matrix and inserts it **inactive** with ``starts_at=now``.
The same cron pass's ``flip_active_events`` then activates and broadcasts it,
so weather reuses the S1 pipeline end to end (prompt injection, A2 collective
memories, TopNav banner, GameScene rendering) with zero new plumbing.

Season comes from the real-world month and shapes both the transition
probabilities (snow exists only in winter; summer storms are more likely) and
the copy text. State machine: sunny ↔ cloudy ↔ rain → storm (+ winter snow) —
storm is only reachable through rain.
"""

import random
from datetime import datetime, timedelta, UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.world_event import WorldEvent

WEATHER_EVENT_TYPE = "weather"

# Real month -> season (northern-hemisphere mapping).
SEASONS: dict[int, str] = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}

# season -> {state -> {next_state -> probability}}. Every row sums to 1.0;
# storm only appears in rain/storm rows (rain → storm chain); snow only in winter.
TRANSITIONS: dict[str, dict[str, dict[str, float]]] = {
    "spring": {
        "sunny":  {"sunny": 0.50, "cloudy": 0.35, "rain": 0.15},
        "cloudy": {"sunny": 0.25, "cloudy": 0.40, "rain": 0.35},
        "rain":   {"cloudy": 0.35, "rain": 0.40, "storm": 0.25},
        "storm":  {"cloudy": 0.30, "rain": 0.50, "storm": 0.20},
    },
    "summer": {
        "sunny":  {"sunny": 0.60, "cloudy": 0.28, "rain": 0.12},
        "cloudy": {"sunny": 0.35, "cloudy": 0.35, "rain": 0.30},
        "rain":   {"cloudy": 0.30, "rain": 0.35, "storm": 0.35},
        "storm":  {"cloudy": 0.25, "rain": 0.45, "storm": 0.30},
    },
    "autumn": {
        "sunny":  {"sunny": 0.50, "cloudy": 0.40, "rain": 0.10},
        "cloudy": {"sunny": 0.25, "cloudy": 0.50, "rain": 0.25},
        "rain":   {"cloudy": 0.40, "rain": 0.40, "storm": 0.20},
        "storm":  {"cloudy": 0.35, "rain": 0.50, "storm": 0.15},
    },
    "winter": {
        "sunny":  {"sunny": 0.45, "cloudy": 0.40, "rain": 0.05, "snow": 0.10},
        "cloudy": {"sunny": 0.20, "cloudy": 0.40, "rain": 0.10, "snow": 0.30},
        "rain":   {"cloudy": 0.35, "rain": 0.30, "storm": 0.15, "snow": 0.20},
        "storm":  {"cloudy": 0.35, "rain": 0.40, "storm": 0.15, "snow": 0.10},
        "snow":   {"sunny": 0.20, "cloudy": 0.35, "snow": 0.45},
    },
}

# kind -> (min, max) sampled intensity, surfaced to the frontend particle layer.
INTENSITY_RANGE: dict[str, tuple[float, float]] = {
    "sunny": (0.0, 0.0),
    "cloudy": (0.3, 0.6),
    "rain": (0.4, 0.8),
    "storm": (0.7, 1.0),
    "snow": (0.4, 0.8),
}

MIN_DURATION_HOURS = 2.0
MAX_DURATION_HOURS = 6.0

# kind -> {season|"default" -> (title, description)}. Description is what the
# S1 pipeline injects into prompts and A2 writes as a collective memory.
WEATHER_COPY: dict[str, dict[str, tuple[str, str]]] = {
    "sunny": {
        "default": ("晴天", "阳光洒满小镇，天气不错。"),
        "spring": ("春日晴天", "春光明媚，花坛边聚了不少居民。"),
        "summer": ("烈日当空", "夏日阳光火辣，树荫下才凉快。"),
        "autumn": ("秋高气爽", "天高云淡，正是出门散步的好天气。"),
        "winter": ("冬日暖阳", "冬天难得出太阳，晒着很舒服。"),
    },
    "cloudy": {
        "default": ("多云", "云层渐厚，天色有些发灰。"),
        "spring": ("春日多云", "薄云遮日，风还是暖的。"),
        "autumn": ("秋阴", "秋云低垂，风里带着凉意。"),
        "winter": ("冬日阴天", "铅灰色的云压得很低，像是要下雪。"),
    },
    "rain": {
        "default": ("下雨", "淅淅沥沥的雨落在小镇屋顶上。"),
        "spring": ("春雨", "春雨绵绵，屋檐一直在滴水。"),
        "summer": ("雷阵雨", "夏日阵雨说来就来，路人纷纷躲进屋檐。"),
        "winter": ("冻雨", "冰冷的雨点砸下来，街上没什么人。"),
    },
    "storm": {
        "default": ("暴风雨", "狂风卷着骤雨，雷声隆隆，居民都躲回了屋里。"),
        "summer": ("夏季雷暴", "闪电划破天空，暴雨倾盆而下。"),
    },
    "snow": {
        "default": ("下雪", "雪花飘落，小镇慢慢披上银装。"),
    },
}

WEATHER_COPY_EN: dict[str, dict[str, tuple[str, str]]] = {
    "sunny": {
        "default": ("Clear Skies", "Sunlight fills the town. It is a fine day."),
        "spring": ("Clear Spring Day", "Warm spring light draws residents toward the flower beds."),
        "summer": ("High Summer Sun", "The summer sun is fierce; the shade is the place to be."),
        "autumn": ("Crisp Autumn Sky", "The air is clear and cool—perfect weather for a walk."),
        "winter": ("Winter Sunshine", "A rare patch of winter sun warms the town."),
    },
    "cloudy": {
        "default": ("Cloudy", "The clouds are thickening and the sky has turned grey."),
        "spring": ("Cloudy Spring Day", "Thin clouds cover the sun, but the breeze remains warm."),
        "autumn": ("Autumn Overcast", "Low autumn clouds bring a chill on the wind."),
        "winter": ("Winter Overcast", "Heavy grey clouds hang low, carrying the promise of snow."),
    },
    "rain": {
        "default": ("Rain", "Steady rain taps across the rooftops."),
        "spring": ("Spring Rain", "A gentle spring rain drips from every eave."),
        "summer": ("Summer Shower", "A sudden summer shower sends everyone under cover."),
        "winter": ("Freezing Rain", "Cold rain strikes the empty streets."),
    },
    "storm": {
        "default": ("Storm", "Wind drives heavy rain through town as thunder rolls overhead."),
        "summer": ("Summer Thunderstorm", "Lightning cuts across the sky and rain pours down."),
    },
    "snow": {"default": ("Snow", "Snowflakes settle over the town in a coat of white.")},
}


def season_for_month(month: int) -> str:
    """Map a real-world month (1-12) to the in-world season."""
    return SEASONS[month]


def sample_next_kind(prev_kind: str, season: str, rng=random) -> str:
    """Draw the next weather kind from the seasonal transition matrix.

    An unknown ``prev_kind`` for the season (e.g. snow after winter ended)
    falls back to the "cloudy" row.
    """
    matrix = TRANSITIONS[season]
    row = matrix.get(prev_kind) or matrix["cloudy"]
    roll = rng.random()
    acc = 0.0
    for kind, prob in row.items():
        acc += prob
        if roll < acc:
            return kind
    return next(iter(row))  # float-rounding fallback; rows sum to 1.0


def sample_segment(prev_kind: str, season: str, rng=random) -> tuple[str, float, float]:
    """Sample (kind, intensity, duration_hours) for the next weather segment."""
    kind = sample_next_kind(prev_kind, season, rng)
    lo, hi = INTENSITY_RANGE[kind]
    intensity = round(rng.uniform(lo, hi), 2) if hi > lo else lo
    duration_hours = round(rng.uniform(MIN_DURATION_HOURS, MAX_DURATION_HOURS), 2)
    return kind, intensity, duration_hours


def weather_copy(kind: str, season: str) -> tuple[str, str]:
    """Season-flavored (title, description) for a weather kind."""
    table = WEATHER_COPY[kind]
    return table.get(season, table["default"])


async def ensure_weather_event(
    db: AsyncSession, now: datetime | None = None, rng=random,
) -> WorldEvent | None:
    """Schedule the next weather segment if none is running or pending.

    A weather event whose ``ends_at`` is still in the future — active or not
    yet flipped — counts as covering the present, so at most one segment exists
    at a time. Created **inactive**: the S1 event_cron flip activates and
    broadcasts it (same pattern as A2 holiday/news templates). Returns the
    created event, or None when the sky is already spoken for.
    """
    now = now or datetime.now(UTC)

    pending = (await db.execute(
        select(WorldEvent.id)
        .where(WorldEvent.type == WEATHER_EVENT_TYPE, WorldEvent.ends_at > now)
        .limit(1)
    )).scalar_one_or_none()
    if pending is not None:
        return None

    prev = (await db.execute(
        select(WorldEvent)
        .where(WorldEvent.type == WEATHER_EVENT_TYPE)
        .order_by(WorldEvent.ends_at.desc())
        .limit(1)
    )).scalars().first()
    prev_kind = (prev.payload_json or {}).get("kind", "sunny") if prev else "sunny"

    season = season_for_month(now.month)
    kind, intensity, duration_hours = sample_segment(prev_kind, season, rng)
    title, description = weather_copy(kind, season)
    en_table = WEATHER_COPY_EN[kind]
    title_en, description_en = en_table.get(season, en_table["default"])

    event = WorldEvent(
        type=WEATHER_EVENT_TYPE,
        title=title,
        description=description,
        payload_json={
            "kind": kind, "intensity": intensity, "season": season,
            "title_en": title_en, "description_en": description_en,
        },
        starts_at=now,
        ends_at=now + timedelta(hours=duration_hours),
        is_active=False,
    )
    db.add(event)
    await db.commit()
    return event


async def get_current_weather(db: AsyncSession) -> dict | None:
    """payload_json ({kind, intensity, season}) of the active weather event, or None.

    Reads through the S1 60s active-event cache — safe to call once per agent
    tick round without extra DB pressure.
    """
    from app.services.world_event_service import get_active_events_cached

    for e in await get_active_events_cached(db):
        if e.get("type") == WEATHER_EVENT_TYPE:
            return e.get("payload_json") or {}
    return None
