"""World self-modification governance (P3): world_change_proposals + the
overlay tables dynamic_locations / dynamic_mechanics.

WRITE ONLY — do not run during burn-in. Verify on real Postgres before deploy.
Chains onto 032_add_lab_core.

Revision ID: 033_add_world_governance
Revises: 032_add_lab_core
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "033_add_world_governance"
down_revision = "032_add_lab_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "world_change_proposals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("origin", sa.String(length=20), nullable=False, server_default="lab_run"),
        sa.Column("origin_ref", sa.String(), nullable=True),
        sa.Column("author_slug", sa.String(length=100), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("rationale_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("patch_json", sa.JSON(), nullable=True),
        sa.Column("cost_sc", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("risk_level", sa.String(length=10), nullable=False, server_default="low"),
        sa.Column("reviewer_id", sa.String(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_world_change_proposals_status", "world_change_proposals", ["status"])

    op.create_table(
        "dynamic_locations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("proposal_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dynamic_locations_slug", "dynamic_locations", ["slug"], unique=True)

    op.create_table(
        "dynamic_mechanics",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("proposal_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dynamic_mechanics_code", "dynamic_mechanics", ["code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_dynamic_mechanics_code", table_name="dynamic_mechanics")
    op.drop_table("dynamic_mechanics")
    op.drop_index("ix_dynamic_locations_slug", table_name="dynamic_locations")
    op.drop_table("dynamic_locations")
    op.drop_index("ix_world_change_proposals_status", table_name="world_change_proposals")
    op.drop_table("world_change_proposals")
