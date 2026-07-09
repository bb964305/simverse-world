"""Add follows + feed_events tables (E11 follow feed).

Two-table create/drop — verify on real Postgres before deploy (vm212).
Chains onto 025_add_time_capsules.

Revision ID: 026_add_follows_feed
Revises: 025_add_time_capsules
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "026_add_follows_feed"
down_revision = "025_add_time_capsules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "follows",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("resident_slug", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "resident_slug", name="uq_follow_user_resident"),
    )
    op.create_index("ix_follows_user_id", "follows", ["user_id"])
    op.create_index("ix_follows_resident_slug", "follows", ["resident_slug"])
    op.create_table(
        "feed_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("resident_slug", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_feed_events_resident_slug", "feed_events", ["resident_slug"])
    op.create_index("ix_feed_events_created_at", "feed_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_feed_events_created_at", table_name="feed_events")
    op.drop_index("ix_feed_events_resident_slug", table_name="feed_events")
    op.drop_table("feed_events")
    op.drop_index("ix_follows_resident_slug", table_name="follows")
    op.drop_index("ix_follows_user_id", table_name="follows")
    op.drop_table("follows")
