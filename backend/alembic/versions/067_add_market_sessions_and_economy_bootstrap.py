"""Add player market receipts, Lab candidates, and audited economy bootstrap.

Revision ID: 067_market_economy_loop
Revises: 066_hosted_agent_controllers
Create Date: 2026-08-15
"""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa


revision = "067_market_economy_loop"
down_revision = "066_hosted_agent_controllers"
branch_labels = None
depends_on = None


_MARKET_RECEIPTS = (
    {
        "code": "market_tea_chest",
        "name": "远行茶箱",
        "description": "商队茶叶留下的收藏茶箱，可摆在家中",
        "icon": "🍵",
        "payload_json": {"market_receipt": True, "sprite": "tea_chest"},
    },
    {
        "code": "market_trinket_display",
        "name": "异乡小玩意",
        "description": "来自远方商路的收藏摆件",
        "icon": "🎁",
        "payload_json": {"market_receipt": True, "sprite": "trinket"},
    },
    {
        "code": "market_cloth_roll",
        "name": "花布卷",
        "description": "可陈列在家中的异乡花布",
        "icon": "🧵",
        "payload_json": {"market_receipt": True, "sprite": "cloth_roll"},
    },
    {
        "code": "market_foreign_lantern",
        "name": "异域工匠灯",
        "description": "商队工匠现场制作的限量灯饰",
        "icon": "🏮",
        "payload_json": {"market_receipt": True, "sprite": "foreign_lantern"},
    },
)


def upgrade() -> None:
    op.create_table(
        "economy_bootstrap_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("bootstrap_key", sa.String(length=100), nullable=False),
        sa.Column("requested_by_user_id", sa.String(), nullable=False),
        sa.Column("resident_floor_sc", sa.Integer(), nullable=False),
        sa.Column("town_target_sc", sa.Integer(), nullable=False),
        sa.Column("town_grant_sc", sa.Integer(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("resident_floor_sc >= 0", name="ck_economy_bootstrap_floor"),
        sa.CheckConstraint("town_target_sc >= 0", name="ck_economy_bootstrap_town_target"),
        sa.CheckConstraint("town_grant_sc >= 0", name="ck_economy_bootstrap_town_grant"),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bootstrap_key", name="uq_economy_bootstrap_key"),
    )
    op.create_table(
        "economy_bootstrap_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("resident_slug", sa.String(length=100), nullable=False),
        sa.Column("amount_sc", sa.Integer(), nullable=False),
        sa.Column("balance_before_sc", sa.Integer(), nullable=False),
        sa.Column("balance_after_sc", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_sc > 0", name="ck_economy_bootstrap_grant_amount"),
        sa.CheckConstraint(
            "balance_before_sc >= 0 AND balance_after_sc >= balance_before_sc",
            name="ck_economy_bootstrap_grant_balances",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["economy_bootstrap_batches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id", "resident_slug", name="uq_economy_bootstrap_resident"
        ),
    )
    op.create_index(
        "ix_economy_bootstrap_grants_batch_id",
        "economy_bootstrap_grants",
        ["batch_id"],
    )
    op.create_index(
        "ix_economy_bootstrap_grants_resident_slug",
        "economy_bootstrap_grants",
        ["resident_slug"],
    )

    op.create_table(
        "caravan_market_purchases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("visit_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("request_key", sa.String(length=64), nullable=False),
        sa.Column("offer_code", sa.String(length=80), nullable=False),
        sa.Column("offer_type", sa.String(length=20), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("total_sc", sa.Integer(), nullable=False),
        sa.Column("effect_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("qty > 0", name="ck_market_purchase_qty"),
        sa.CheckConstraint("total_sc >= 0", name="ck_market_purchase_total"),
        sa.CheckConstraint(
            "offer_type IN ('good','service','contract')",
            name="ck_market_purchase_offer_type",
        ),
        sa.ForeignKeyConstraint(
            ["visit_id"], ["caravan_visits.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "request_key", name="uq_market_purchase_user_request"
        ),
        sa.UniqueConstraint(
            "visit_id", "user_id", "offer_code",
            name="uq_market_purchase_visit_user_offer",
        ),
    )
    op.create_index(
        "ix_caravan_market_purchases_visit_id",
        "caravan_market_purchases",
        ["visit_id"],
    )
    op.create_index(
        "ix_caravan_market_purchases_user_id",
        "caravan_market_purchases",
        ["user_id"],
    )

    op.create_table(
        "lab_market_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("proposed_by_user_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("offer_type", sa.String(length=20), nullable=False),
        sa.Column("suggested_price_sc", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reviewed_by_user_id", sa.String(), nullable=True),
        sa.Column("review_note", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','published')",
            name="ck_lab_market_candidate_status",
        ),
        sa.CheckConstraint(
            "offer_type IN ('good','service','contract')",
            name="ck_lab_market_candidate_offer_type",
        ),
        sa.CheckConstraint(
            "suggested_price_sc >= 0", name="ck_lab_market_candidate_price"
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["lab_artifacts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["lab_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proposed_by_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", name="uq_lab_market_candidate_artifact"),
    )
    op.create_index(
        "ix_lab_market_candidates_artifact_id",
        "lab_market_candidates",
        ["artifact_id"],
    )
    op.create_index(
        "ix_lab_market_candidates_task_id",
        "lab_market_candidates",
        ["task_id"],
    )
    op.create_index(
        "ix_lab_market_candidates_status",
        "lab_market_candidates",
        ["status"],
    )

    items = sa.table(
        "items",
        sa.column("id", sa.String()),
        sa.column("code", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("icon", sa.String()),
        sa.column("price_sc", sa.Integer()),
        sa.column("payload_json", sa.JSON()),
        sa.column("stock", sa.Integer()),
        sa.column("active", sa.Boolean()),
    )
    op.bulk_insert(
        items,
        [
            {
                "id": str(uuid.uuid4()),
                "code": definition["code"],
                "kind": "decor",
                "name": definition["name"],
                "description": definition["description"],
                "icon": definition["icon"],
                "price_sc": 0,
                "payload_json": definition["payload_json"],
                "stock": None,
                "active": False,
            }
            for definition in _MARKET_RECEIPTS
        ],
    )


def downgrade() -> None:
    codes = [definition["code"] for definition in _MARKET_RECEIPTS]
    items = sa.table("items", sa.column("code", sa.String()))
    op.execute(items.delete().where(items.c.code.in_(codes)))
    op.drop_index("ix_lab_market_candidates_status", table_name="lab_market_candidates")
    op.drop_index("ix_lab_market_candidates_task_id", table_name="lab_market_candidates")
    op.drop_index("ix_lab_market_candidates_artifact_id", table_name="lab_market_candidates")
    op.drop_table("lab_market_candidates")
    op.drop_index("ix_caravan_market_purchases_user_id", table_name="caravan_market_purchases")
    op.drop_index("ix_caravan_market_purchases_visit_id", table_name="caravan_market_purchases")
    op.drop_table("caravan_market_purchases")
    op.drop_index("ix_economy_bootstrap_grants_resident_slug", table_name="economy_bootstrap_grants")
    op.drop_index("ix_economy_bootstrap_grants_batch_id", table_name="economy_bootstrap_grants")
    op.drop_table("economy_bootstrap_grants")
    op.drop_table("economy_bootstrap_batches")
