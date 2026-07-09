"""Add commissions table (B1 commission tasks).

Single-table create/drop — verify on real Postgres before deploy (vm212).
Chains onto 021_add_daily_loop.

Revision ID: 022_add_commissions
Revises: 021_add_daily_loop
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "022_add_commissions"
down_revision = "021_add_daily_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commissions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("issuer_resident_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("reward_sc", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("acceptor_user_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_commissions_issuer_resident_id", "commissions", ["issuer_resident_id"])
    op.create_index("ix_commissions_status", "commissions", ["status"])
    op.create_index("ix_commissions_acceptor_user_id", "commissions", ["acceptor_user_id"])


def downgrade() -> None:
    op.drop_index("ix_commissions_acceptor_user_id", table_name="commissions")
    op.drop_index("ix_commissions_status", table_name="commissions")
    op.drop_index("ix_commissions_issuer_resident_id", table_name="commissions")
    op.drop_table("commissions")
