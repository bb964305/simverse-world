"""Add resident_goals table (A1 life goals / story arcs).

Single-table create/drop — verify on real Postgres before deploy (vm212).
Chains onto 022_add_commissions.

Revision ID: 023_add_resident_goals
Revises: 022_add_commissions
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "023_add_resident_goals"
down_revision = "022_add_commissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resident_goals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("resident_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="life"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("motivation", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("milestones_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_resident_goals_resident_id", "resident_goals", ["resident_id"])


def downgrade() -> None:
    op.drop_index("ix_resident_goals_resident_id", table_name="resident_goals")
    op.drop_table("resident_goals")
