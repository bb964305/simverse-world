"""Add seasons family: seasons + season_scripts + polls + votes + season_scores (C3/E12).

Five-table create/drop — verify on real Postgres before deploy (vm212).
Chains onto 026_add_follows_feed.

Revision ID: 027_add_seasons
Revises: 026_add_follows_feed
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "027_add_seasons"
down_revision = "026_add_follows_feed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "seasons",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("theme", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="voting"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
    )
    op.create_table(
        "season_scripts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("season_id", sa.String(), nullable=False),
        sa.Column("act", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("trigger_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_payload_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
    )
    op.create_index("ix_season_scripts_season_id", "season_scripts", ["season_id"])
    op.create_table(
        "polls",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("season_id", sa.String(), nullable=True),
        sa.Column("question", sa.String(length=300), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=True),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
    )
    op.create_table(
        "votes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("poll_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("option_idx", sa.Integer(), nullable=False),
        sa.UniqueConstraint("poll_id", "user_id", name="uq_vote_poll_user"),
    )
    op.create_index("ix_votes_poll_id", "votes", ["poll_id"])
    op.create_table(
        "season_scores",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("season_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("breakdown_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("season_id", "user_id", name="uq_season_score"),
    )
    op.create_index("ix_season_scores_season_id", "season_scores", ["season_id"])
    op.create_index("ix_season_scores_user_id", "season_scores", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_season_scores_user_id", table_name="season_scores")
    op.drop_index("ix_season_scores_season_id", table_name="season_scores")
    op.drop_table("season_scores")
    op.drop_index("ix_votes_poll_id", table_name="votes")
    op.drop_table("votes")
    op.drop_table("polls")
    op.drop_index("ix_season_scripts_season_id", table_name="season_scripts")
    op.drop_table("season_scripts")
    op.drop_table("seasons")
