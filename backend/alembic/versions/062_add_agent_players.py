"""Add external Agent player identities and hashed credentials.

Revision ID: 062_add_agent_players
Revises: 061_add_caravan_market_visitors
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa


revision = "062_add_agent_players"
down_revision = "061_add_caravan_market_visitors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_players",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("resident_id", sa.String(), nullable=False),
        sa.Column("control_kind", sa.String(length=32), nullable=False),
        sa.Column("model_label", sa.String(length=100), nullable=True),
        sa.Column("client_json", sa.JSON(), nullable=False),
        sa.Column("role_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("public_visible", sa.Boolean(), nullable=False),
        sa.Column("observation_seq", sa.Integer(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("last_seen_event_seq", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["resident_id"], ["residents.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resident_id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index("ix_agent_players_last_seen_at", "agent_players", ["last_seen_at"])
    op.create_index("ix_agent_players_status", "agent_players", ["status"])

    op.create_table(
        "agent_credentials",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_player_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_player_id"], ["agent_players.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_agent_credentials_token_hash"),
    )
    op.create_index(
        "ix_agent_credentials_agent_player_id", "agent_credentials", ["agent_player_id"]
    )
    op.create_index("ix_agent_credentials_expires_at", "agent_credentials", ["expires_at"])
    op.create_index("ix_agent_credentials_kind", "agent_credentials", ["kind"])

    op.create_table(
        "agent_action_receipts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_player_id", sa.String(), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("observation_seq", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_player_id"], ["agent_players.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_player_id", "action_id", name="uq_agent_action_agent_action_id"
        ),
    )
    op.create_index(
        "ix_agent_action_receipts_agent_player_id",
        "agent_action_receipts",
        ["agent_player_id"],
    )

    op.create_table(
        "agent_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_player_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_player_id"], ["agent_players.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_player_id", "sequence", name="uq_agent_events_agent_sequence"
        ),
    )
    op.create_index("ix_agent_events_agent_player_id", "agent_events", ["agent_player_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_events_agent_player_id", table_name="agent_events")
    op.drop_table("agent_events")
    op.drop_index(
        "ix_agent_action_receipts_agent_player_id", table_name="agent_action_receipts"
    )
    op.drop_table("agent_action_receipts")
    op.drop_index("ix_agent_credentials_kind", table_name="agent_credentials")
    op.drop_index("ix_agent_credentials_expires_at", table_name="agent_credentials")
    op.drop_index("ix_agent_credentials_agent_player_id", table_name="agent_credentials")
    op.drop_table("agent_credentials")
    op.drop_index("ix_agent_players_status", table_name="agent_players")
    op.drop_index("ix_agent_players_last_seen_at", table_name="agent_players")
    op.drop_table("agent_players")
