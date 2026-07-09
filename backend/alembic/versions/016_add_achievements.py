"""Add achievements + user_achievements tables (S2 achievement engine).

Two-table create/drop — verify `upgrade`/`downgrade -1` on real Postgres before
deploy (vm212). Base-week numbering shifted +2 (see 014).

Revision ID: 016_add_achievements
Revises: 015_add_notifications
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "016_add_achievements"
down_revision = "015_add_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "achievements",
        sa.Column("code", sa.String(length=50), primary_key=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("icon", sa.String(length=20), nullable=False, server_default="🏆"),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reward_sc", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "user_achievements",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("progress_json", sa.JSON(), nullable=True),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "code", name="uq_user_achievement"),
    )
    op.create_index("ix_user_achievements_user_id", "user_achievements", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_achievements_user_id", table_name="user_achievements")
    op.drop_table("user_achievements")
    op.drop_table("achievements")
