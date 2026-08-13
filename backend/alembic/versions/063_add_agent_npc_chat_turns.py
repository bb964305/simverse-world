"""Add durable Agent-to-NPC single-turn receipts.

Revision ID: 063_agent_npc_chat_turns
Revises: 062_add_agent_players
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa


revision = "063_agent_npc_chat_turns"
down_revision = "062_add_agent_players"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_players", sa.Column("operation_kind", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "agent_players", sa.Column("operation_token", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "agent_players",
        sa.Column("operation_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "agent_npc_chat_turn_receipts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_player_id", sa.String(), nullable=False),
        sa.Column("resident_id", sa.String(), nullable=True),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("observation_seq", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("recovery_json", sa.JSON(), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_player_id"], ["agent_players.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["resident_id"], ["residents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_player_id",
            "turn_id",
            name="uq_agent_npc_chat_turn_agent_turn_id",
        ),
    )
    op.create_index(
        "ix_agent_npc_chat_turn_receipts_agent_player_id",
        "agent_npc_chat_turn_receipts",
        ["agent_player_id"],
    )
    op.create_index(
        "ix_agent_npc_chat_turn_receipts_resident_id",
        "agent_npc_chat_turn_receipts",
        ["resident_id"],
    )
    op.create_index(
        "ix_agent_npc_chat_turn_receipts_conversation_id",
        "agent_npc_chat_turn_receipts",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_npc_chat_turn_receipts_conversation_id",
        table_name="agent_npc_chat_turn_receipts",
    )
    op.drop_index(
        "ix_agent_npc_chat_turn_receipts_resident_id",
        table_name="agent_npc_chat_turn_receipts",
    )
    op.drop_index(
        "ix_agent_npc_chat_turn_receipts_agent_player_id",
        table_name="agent_npc_chat_turn_receipts",
    )
    op.drop_table("agent_npc_chat_turn_receipts")
    op.drop_column("agent_players", "operation_expires_at")
    op.drop_column("agent_players", "operation_token")
    op.drop_column("agent_players", "operation_kind")
