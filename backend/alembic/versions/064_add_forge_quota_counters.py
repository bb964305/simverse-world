"""Add concurrency-safe UGC/Forge quotas and Forge slug reservations.

Revision ID: 064_forge_quota_counters
Revises: 063_agent_npc_chat_turns
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa


revision = "064_forge_quota_counters"
down_revision = "063_agent_npc_chat_turns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("ugc_creation_date", sa.Date(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "ugc_creation_count", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column("users", sa.Column("forge_reward_date", sa.Date(), nullable=True))
    op.add_column(
        "users",
        sa.Column("forge_reward_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "forge_sessions", sa.Column("target_slug", sa.String(length=100), nullable=True)
    )
    op.create_index(
        "ix_forge_sessions_target_slug",
        "forge_sessions",
        ["target_slug"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_forge_sessions_target_slug", table_name="forge_sessions")
    op.drop_column("forge_sessions", "target_slug")
    op.drop_column("users", "forge_reward_count")
    op.drop_column("users", "forge_reward_date")
    op.drop_column("users", "ugc_creation_count")
    op.drop_column("users", "ugc_creation_date")
