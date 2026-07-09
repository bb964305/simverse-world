"""Add time_capsules table (E7 time capsule letters).

Single-table create/drop — verify on real Postgres before deploy (vm212).
Chains onto 024_add_bulletin_posts.

Revision ID: 025_add_time_capsules
Revises: 024_add_bulletin_posts
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "025_add_time_capsules"
down_revision = "024_add_bulletin_posts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "time_capsules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("carrier_resident_slug", sa.String(length=100), nullable=False),
        sa.Column("deliver_on", sa.Date(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("resident_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="sealed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_time_capsules_user_id", "time_capsules", ["user_id"])
    op.create_index("ix_time_capsules_deliver_on", "time_capsules", ["deliver_on"])


def downgrade() -> None:
    op.drop_index("ix_time_capsules_deliver_on", table_name="time_capsules")
    op.drop_index("ix_time_capsules_user_id", table_name="time_capsules")
    op.drop_table("time_capsules")
