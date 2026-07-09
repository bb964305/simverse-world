"""Add daily-loop fields: users.login_streak/last_login_date + daily_quests (D3).

Verify on real Postgres before deploy (vm212). Chains onto 020_add_resident_mood.

Revision ID: 021_add_daily_loop
Revises: 020_add_resident_mood
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "021_add_daily_loop"
down_revision = "020_add_resident_mood"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("login_streak", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("last_login_date", sa.Date(), nullable=True))
    op.create_table(
        "daily_quests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("quest_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("reward_sc", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "date", name="uq_daily_quest_user_date"),
    )
    op.create_index("ix_daily_quests_user_id", "daily_quests", ["user_id"])
    op.create_index("ix_daily_quests_date", "daily_quests", ["date"])


def downgrade() -> None:
    op.drop_index("ix_daily_quests_date", table_name="daily_quests")
    op.drop_index("ix_daily_quests_user_id", table_name="daily_quests")
    op.drop_table("daily_quests")
    op.drop_column("users", "last_login_date")
    op.drop_column("users", "login_streak")
