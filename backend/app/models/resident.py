import uuid
from datetime import datetime, UTC
from sqlalchemy import String, Integer, Float, DateTime, Text, JSON, ForeignKey
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Resident(Base):
    __tablename__ = "residents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    district: Mapped[str] = mapped_column(String(50), default="free")
    status: Mapped[str] = mapped_column(String(20), default="idle", index=True)  # P1-3: hot filter
    heat: Mapped[int] = mapped_column(Integer, default=0)
    # Realism P0-5a: manual/pinned heat floor. `heat` is the real value (may fall
    # as the 7-day conversation window slides); display = max(heat, pinned_heat);
    # state decisions use the real value only.
    pinned_heat: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    model_tier: Mapped[str] = mapped_column(String(20), default="standard")
    token_cost_per_turn: Mapped[int] = mapped_column(Integer, default=1)
    # Nullable: account deletion orphans owned residents (creator_id → NULL,
    # migration 040) instead of deleting them from the world.
    creator_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), index=True, nullable=True
    )
    ability_md: Mapped[str] = mapped_column(Text, default="")
    persona_md: Mapped[str] = mapped_column(Text, default="")
    soul_md: Mapped[str] = mapped_column(Text, default="")
    # meta_json holds loosely-typed sub-namespaces keyed by feature, e.g.
    #   "sbti": {...}
    #   "lab":  {"access": bool, "tier": str, "skills": [str, ...]}
    #     — Lab/experiment building (spec §2). "access" is the admin-granted
    #       researcher whitelist flag gating ActionType.RESEARCH + LabTask
    #       assignment; "tier"/"skills" drive auto-dispatch of open recruitment.
    #       The researcher's treasury balance is NOT stored here — it lives in
    #       the resident_treasuries table (spec §4.7, atomic + auditable), a
    #       v0.2 revision away from the earlier meta_json plan.
    meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    versions_json: Mapped[list] = mapped_column(JSON, default=list, nullable=True)
    sprite_key: Mapped[str] = mapped_column(String(100), default="伊莎贝拉")
    tile_x: Mapped[int] = mapped_column(Integer, default=76)  # Default spawn: Central Plaza
    tile_y: Mapped[int] = mapped_column(Integer, default=50)
    star_rating: Mapped[int] = mapped_column(Integer, default=1)
    total_conversations: Mapped[int] = mapped_column(Integer, default=0)
    avg_rating: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    last_conversation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    search_vector: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- New fields (Plan 1: Foundation) ---
    resident_type: Mapped[str] = mapped_column(String(20), default="npc")
    reply_mode: Mapped[str] = mapped_column(String(20), default="manual")
    portrait_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Agent fields (P3: Agent Loop) ---
    home_tile_x: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_tile_y: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Agent Planning (Plugin System) ---
    daily_goal_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    daily_plans_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Housing (Map Awareness) ---
    home_location_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # --- Emotion engine (E1) ---
    # {"valence": -1..1, "arousal": 0..1, "label": str, "updated_at": iso}
    mood_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Home decor (B3, migration 031) ---
    # [{item_code, x, y, rot}] — x/y are tile offsets relative to the
    # top-left corner of the home_location_id bbox (map_data bounds).
    home_decor_json: Mapped[list | None] = mapped_column(JSON, nullable=True)

    @property
    def mood_label(self) -> str:
        """Current mood word (E1 label set); residents without mood are calm."""
        return str((self.mood_json or {}).get("label") or "calm")

    @property
    def display_heat(self) -> int:
        """Realism P0-5a: shown heat = max(real heat, pinned floor)."""
        return max(self.heat or 0, self.pinned_heat or 0)

    @hybrid_property
    def is_autonomous(self) -> bool:
        """Whether this resident may be driven by NPC-only world systems.

        ``resident_type`` remains the persisted identity field; this hybrid is
        the canonical eligibility predicate for both loaded objects and SQL
        queries. Player residents are registered members of the world, but are
        never autonomous actors.
        """
        return self.resident_type == "npc"

    @is_autonomous.inplace.expression
    @classmethod
    def _is_autonomous_expression(cls):
        return cls.resident_type == "npc"
