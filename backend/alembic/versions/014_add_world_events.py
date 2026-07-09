"""Add world_events table (S1 base bus).

Time-boxed world events (festival/weather/news/custom/script); a 60s cron flips
is_active and broadcasts transitions. Single-table create/drop — verify
`upgrade`/`downgrade -1` on real Postgres before deploy (vm212).

Note: base-week migration numbers were shifted +2 from the spec's 012-016 —
012/013 were already taken by sync_schema_drift / llm_usage — so world_events is
014 and chains onto 013_add_llm_usage (spec §40 allows non-numeric ordering).

Revision ID: 014_add_world_events
Revises: 013_add_llm_usage
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "014_add_world_events"
down_revision = "013_add_llm_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "world_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_world_events_is_active", "world_events", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_world_events_is_active", table_name="world_events")
    op.drop_table("world_events")
