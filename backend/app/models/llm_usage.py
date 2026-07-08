import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, Float, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LLMUsage(Base):
    """Append-only per-attempt LLM telemetry (P1-1, E-19/E-23).

    One row per LLM *attempt* (not per success): retries and parse-failure
    fallbacks are the pure waste we most want to see, so charging is recorded
    even when ``parse_ok`` is False (E-19).

    Deliberately has **no foreign keys**. This is a high-write telemetry table
    whose rows must (a) never couple the business transaction that spawned the
    call — metering writes go to their own short-lived session so a caller
    rollback or a metering failure can never poison the real work — and
    (b) outlive the resident / user / conversation they reference. Storing the
    ids as plain indexed columns (not FKs) is exactly the shape that dodged the
    FK-insert-order and type-drift bugs that bit the real Postgres run on vm212.
    """

    __tablename__ = "llm_usage"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )

    # What was called
    scenario: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(80))
    owner: Mapped[str] = mapped_column(String(16))  # "system" | "user"

    # Who it was for (plain indexed columns, NOT foreign keys — see class docstring)
    resident_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    # Attempt-level quality signal
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    parse_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Token accounting (cache_* reserved: caching is currently disabled, E-01/E-07)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)

    # "usage"     -> read from response.usage (accurate)
    # "estimated" -> endpoint omitted usage; shadow-metered from char heuristic
    source: Mapped[str] = mapped_column(String(16), default="usage")

    # Materialised cost so the budget circuit breaker can SUM() over a window
    # without a price join. Computed in Python at write time (see llm.pricing).
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
