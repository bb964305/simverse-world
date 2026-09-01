"""Add a unique EVM wallet identity to users.

Revision ID: 069_wallet_identity
Revises: 068_fix_theater_bounds
Create Date: 2026-09-01
"""

from alembic import op
import sqlalchemy as sa


revision = "069_wallet_identity"
down_revision = "068_fix_theater_bounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("wallet_address", sa.String(length=42), nullable=True))
    op.create_index("ix_users_wallet_address", "users", ["wallet_address"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_wallet_address", table_name="users")
    op.drop_column("users", "wallet_address")
