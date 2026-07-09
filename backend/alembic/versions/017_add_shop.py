"""Add items + purchases tables (S3 shop pipeline).

Two-table create/drop — verify `upgrade`/`downgrade -1` on real Postgres before
deploy (vm212). Base-week numbering shifted +2 (see 014).

Revision ID: 017_add_shop
Revises: 016_add_achievements
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "017_add_shop"
down_revision = "016_add_achievements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("icon", sa.String(length=20), nullable=False, server_default="📦"),
        sa.Column("price_sc", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_items_code", "items", ["code"], unique=True)
    op.create_table(
        "purchases",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("item_code", sa.String(length=50), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("total_sc", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_purchases_user_id", "purchases", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_purchases_user_id", table_name="purchases")
    op.drop_table("purchases")
    op.drop_index("ix_items_code", table_name="items")
    op.drop_table("items")
