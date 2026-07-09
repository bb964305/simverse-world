"""Add goal_investments table (E13 goal investment).

Single-table create/drop — verify on real Postgres before deploy (vm212).
Chains onto 027_add_seasons.

Revision ID: 028_add_goal_investments
Revises: 027_add_seasons
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "028_add_goal_investments"
down_revision = "027_add_seasons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "goal_investments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("goal_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("payout", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_goal_investments_goal_id", "goal_investments", ["goal_id"])
    op.create_index("ix_goal_investments_user_id", "goal_investments", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_goal_investments_user_id", table_name="goal_investments")
    op.drop_index("ix_goal_investments_goal_id", table_name="goal_investments")
    op.drop_table("goal_investments")
