"""Add debates + debate_stakes tables (E9 debate arena).

Two-table create/drop — verify on real Postgres before deploy (vm212).
Chains onto 028_add_goal_investments.

Revision ID: 029_add_debates
Revises: 028_add_goal_investments
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "029_add_debates"
down_revision = "028_add_goal_investments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "debates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("topic", sa.String(length=300), nullable=False),
        sa.Column("resident_a_slug", sa.String(length=100), nullable=False),
        sa.Column("resident_b_slug", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="announced"),
        sa.Column("transcript_json", sa.JSON(), nullable=True),
        sa.Column("winner", sa.String(length=10), nullable=True),
        sa.Column("pool_a", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pool_b", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("votes_a", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("votes_b", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "debate_stakes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("debate_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("payout", sa.Integer(), nullable=True),
        sa.UniqueConstraint("debate_id", "user_id", name="uq_debate_stake"),
    )
    op.create_index("ix_debate_stakes_debate_id", "debate_stakes", ["debate_id"])


def downgrade() -> None:
    op.drop_index("ix_debate_stakes_debate_id", table_name="debate_stakes")
    op.drop_table("debate_stakes")
    op.drop_table("debates")
