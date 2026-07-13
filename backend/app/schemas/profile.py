from datetime import datetime
from pydantic import BaseModel


class MyResidentItem(BaseModel):
    id: str
    slug: str
    name: str
    district: str
    status: str
    heat: int
    star_rating: int
    total_conversations: int
    avg_rating: float
    sprite_key: str
    meta_json: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}


class MyConversationItem(BaseModel):
    id: str
    resident_id: str
    resident_name: str
    resident_slug: str
    started_at: datetime
    ended_at: datetime | None
    turns: int
    rating: int | None


class MyTransactionItem(BaseModel):
    id: str
    amount: int
    reason: str
    created_at: datetime


# ── D4 Creator dashboard (GET /creator/stats) ──────────────────────────────

class SeriesPoint(BaseModel):
    """One day of a time series ('YYYY-MM-DD' → integer value)."""
    date: str
    value: int


class WeeklyRatingPoint(BaseModel):
    """One Monday-aligned week bucket of the rating trend."""
    week_start: str
    avg_rating: float | None
    count: int


class CreatorResidentStats(BaseModel):
    id: str
    slug: str
    name: str
    sprite_key: str
    star_rating: int
    conversations_30d: int
    avg_rating_30d: float | None
    earnings_30d: int
    memories_30d: int
    # Sparse: only days with ≥1 conversation (frontend zero-fills the window).
    daily_conversations: list[SeriesPoint]


class CreatorTotals(BaseModel):
    conversations: int
    earnings_sc: int
    memories: int
    avg_rating: float | None


class CreatorStatsResponse(BaseModel):
    window_days: int
    since: str  # first day of the window, 'YYYY-MM-DD'
    residents: list[CreatorResidentStats]
    # Zero-filled series covering the whole window (empty when no residents).
    daily_conversations: list[SeriesPoint]
    daily_earnings: list[SeriesPoint]
    weekly_ratings: list[WeeklyRatingPoint]
    totals: CreatorTotals
