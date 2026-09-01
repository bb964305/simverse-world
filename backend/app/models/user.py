import uuid
from datetime import datetime, date as date_type, UTC
from sqlalchemy import String, Integer, Boolean, DateTime, Date, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    github_id: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True)
    # Lower-case EVM address. Checksummed display form is reconstructed at the
    # boundary; lower-case storage makes uniqueness independent of casing.
    wallet_address: Mapped[str | None] = mapped_column(String(42), unique=True, index=True, nullable=True)
    avatar: Mapped[str | None] = mapped_column(String(500), nullable=True)
    soul_coin_balance: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    last_daily_reward_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- Daily loop (D3) ---
    login_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_login_date: Mapped[date_type | None] = mapped_column(Date, nullable=True)
    # Forge quota counters are advanced with conditional UPDATE statements, not
    # read/modify/write ORM updates.  This makes admission/reward limits safe
    # across uvicorn workers and concurrent requests on both PostgreSQL and
    # SQLite. Dates are UTC calendar days.
    ugc_creation_date: Mapped[date_type | None] = mapped_column(Date, nullable=True)
    ugc_creation_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    forge_reward_date: Mapped[date_type | None] = mapped_column(Date, nullable=True)
    forge_reward_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # --- New fields (Plan 1: Foundation) ---
    linuxdo_id: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True)
    linuxdo_trust_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    # use_alter breaks the users<->residents FK cycle so the unit of work can
    # topologically order INSERTs (users must precede transactions/residents).
    player_resident_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("residents.id", use_alter=True), nullable=True
    )
    last_x: Mapped[int] = mapped_column(Integer, default=2432)
    last_y: Mapped[int] = mapped_column(Integer, default=1600)
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    custom_llm_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    custom_llm_api_format: Mapped[str] = mapped_column(String(20), default="anthropic")
    custom_llm_api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    custom_llm_base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    custom_llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
