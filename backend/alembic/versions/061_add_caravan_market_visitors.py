"""Add restart-safe resident market visitors and purchase receipts.

Revision ID: 061_add_caravan_market_visitors
Revises: 060_add_caravan_lifecycle
Create Date: 2026-08-13
"""
from alembic import op
import sqlalchemy as sa


revision = "061_add_caravan_market_visitors"
down_revision = "060_add_caravan_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "caravan_market_visitors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("visit_id", sa.String(length=36), nullable=False),
        sa.Column("resident_id", sa.String(), nullable=False),
        sa.Column("resident_slug", sa.String(length=100), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("item_code", sa.String(length=50), nullable=True),
        sa.Column("spent_sc", sa.Integer(), nullable=True),
        sa.Column("purchase_sequence", sa.Integer(), nullable=True),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["visit_id"], ["caravan_visits.id"],
            name="fk_caravan_market_visitors_visit", ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "slot_index >= 0 AND slot_index < 4",
            name="ck_caravan_market_visitor_slot",
        ),
        sa.CheckConstraint(
            "(item_code IS NULL AND spent_sc IS NULL AND purchase_sequence IS NULL "
            "AND purchased_at IS NULL) OR "
            "(item_code IS NOT NULL AND spent_sc > 0 AND purchase_sequence > 0 "
            "AND purchased_at IS NOT NULL)",
            name="ck_caravan_market_visitor_purchase",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "visit_id", "resident_id", name="uq_caravan_market_visitor_resident"
        ),
        sa.UniqueConstraint(
            "visit_id", "slot_index", name="uq_caravan_market_visitor_slot"
        ),
        sa.UniqueConstraint(
            "visit_id", "purchase_sequence",
            name="uq_caravan_market_visitor_purchase_sequence",
        ),
    )
    op.create_index(
        "ix_caravan_market_visitors_visit_id",
        "caravan_market_visitors",
        ["visit_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_caravan_market_visitors_visit_id",
        table_name="caravan_market_visitors",
    )
    op.drop_table("caravan_market_visitors")
