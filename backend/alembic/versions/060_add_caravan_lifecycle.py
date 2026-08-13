"""Add durable, restart-safe caravan lifecycle tables.

Revision ID: 060_add_caravan_lifecycle
Revises: 059_add_embedding_queue_index
Create Date: 2026-08-12
"""
from alembic import op
import sqlalchemy as sa


revision = "060_add_caravan_lifecycle"
down_revision = "059_add_embedding_queue_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "caravan_visits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("world_event_id", sa.String(), nullable=False),
        sa.Column("phase", sa.String(length=20), nullable=False),
        sa.Column("visibility_slot", sa.String(length=20), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tile_x", sa.Integer(), nullable=False),
        sa.Column("tile_y", sa.Integer(), nullable=False),
        sa.Column("route_json", sa.JSON(), nullable=True),
        sa.Column("motion_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("motion_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fee_sc", sa.Integer(), nullable=False),
        sa.Column("fee_settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imports_stocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imports_withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("departed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["world_event_id"], ["world_events.id"],
            name="fk_caravan_visits_world_event", ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "phase IN ('scheduled','waiting','inbound','trading','outbound','departed','cancelled')",
            name="ck_caravan_visits_phase",
        ),
        sa.CheckConstraint("version >= 1", name="ck_caravan_visits_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "visibility_slot", name="uq_caravan_visits_visibility_slot"
        ),
        sa.UniqueConstraint("world_event_id", name="uq_caravan_visits_world_event_id"),
    )
    op.create_index("ix_caravan_visits_world_event_id", "caravan_visits", ["world_event_id"])
    op.create_index("ix_caravan_visits_phase", "caravan_visits", ["phase"])
    op.create_index("ix_caravan_visits_next_action_at", "caravan_visits", ["next_action_at"])
    op.create_index("ix_caravan_visits_lease_owner", "caravan_visits", ["lease_owner"])
    op.create_index("ix_caravan_visits_lease_expires_at", "caravan_visits", ["lease_expires_at"])
    op.create_index(
        "ix_caravan_visits_due", "caravan_visits", ["phase", "next_action_at"]
    )

    op.create_table(
        "caravan_visit_purchases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("visit_id", sa.String(length=36), nullable=False),
        sa.Column("item_code", sa.String(length=50), nullable=False),
        sa.Column("creator_slug", sa.String(length=100), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("gross_sc", sa.Integer(), nullable=False),
        sa.Column("tax_sc", sa.Integer(), nullable=False),
        sa.Column("net_sc", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["visit_id"], ["caravan_visits.id"],
            name="fk_caravan_purchases_visit", ondelete="CASCADE",
        ),
        sa.CheckConstraint("qty > 0", name="ck_caravan_purchase_qty"),
        sa.CheckConstraint(
            "gross_sc >= 0 AND tax_sc >= 0 AND net_sc >= 0 AND tax_sc + net_sc = gross_sc",
            name="ck_caravan_purchase_amounts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("visit_id", "item_code", name="uq_caravan_purchase_item"),
    )
    op.create_index(
        "ix_caravan_visit_purchases_visit_id", "caravan_visit_purchases", ["visit_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_caravan_visit_purchases_visit_id", table_name="caravan_visit_purchases"
    )
    op.drop_table("caravan_visit_purchases")
    op.drop_index("ix_caravan_visits_due", table_name="caravan_visits")
    op.drop_index("ix_caravan_visits_lease_expires_at", table_name="caravan_visits")
    op.drop_index("ix_caravan_visits_lease_owner", table_name="caravan_visits")
    op.drop_index("ix_caravan_visits_next_action_at", table_name="caravan_visits")
    op.drop_index("ix_caravan_visits_phase", table_name="caravan_visits")
    op.drop_index("ix_caravan_visits_world_event_id", table_name="caravan_visits")
    op.drop_table("caravan_visits")
