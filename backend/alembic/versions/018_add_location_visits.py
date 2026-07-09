"""Add location_visits table (S5 LocationTracker).

Single-table create/drop — verify `upgrade`/`downgrade -1` on real Postgres
before deploy (vm212). Base-week numbering shifted +2 (see 014).

Revision ID: 018_add_location_visits
Revises: 017_add_shop
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "018_add_location_visits"
down_revision = "017_add_shop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "location_visits",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("location_id", sa.String(length=50), nullable=False),
        sa.Column("visit_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_visited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_visited_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "location_id", name="uq_location_visit"),
    )
    op.create_index("ix_location_visits_user_id", "location_visits", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_location_visits_user_id", table_name="location_visits")
    op.drop_table("location_visits")
