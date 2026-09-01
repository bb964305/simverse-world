"""Add durable resident to on-chain Agent Passport links.

Revision ID: 070_web3_agent_passports
Revises: 069_wallet_identity
Create Date: 2026-09-01
"""

from alembic import op
import sqlalchemy as sa


revision = "070_web3_agent_passports"
down_revision = "069_wallet_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web3_agent_passports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("resident_id", sa.String(), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("registry_address", sa.String(length=42), nullable=False),
        sa.Column("agent_id", sa.String(length=78), nullable=False),
        sa.Column("resident_key", sa.String(length=66), nullable=False),
        sa.Column("registration_tx_hash", sa.String(length=66), nullable=True),
        sa.Column("metadata_uri", sa.String(length=1000), nullable=False),
        sa.Column("metadata_hash", sa.String(length=66), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["resident_id"], ["residents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resident_id", name="uq_web3_agent_passport_resident"),
        sa.UniqueConstraint(
            "chain_id", "registry_address", "agent_id",
            name="uq_web3_agent_passport_chain_agent",
        ),
        sa.UniqueConstraint("registration_tx_hash", name="uq_web3_agent_passport_tx"),
    )
    op.create_index(
        "ix_web3_agent_passports_user_id", "web3_agent_passports", ["user_id"]
    )
    op.create_index(
        "ix_web3_agent_passports_resident_id", "web3_agent_passports", ["resident_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_web3_agent_passports_resident_id", table_name="web3_agent_passports")
    op.drop_index("ix_web3_agent_passports_user_id", table_name="web3_agent_passports")
    op.drop_table("web3_agent_passports")
