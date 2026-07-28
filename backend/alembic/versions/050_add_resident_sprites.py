"""Add durable resident sprite review and publication state.

Revision ID: 050_add_resident_sprites
Revises: 049_add_policies
Create Date: 2026-07-26
"""
import sqlalchemy as sa
from alembic import op

revision = "050_add_resident_sprites"
down_revision = "049_add_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("residents") as batch:
        batch.add_column(sa.Column("sprite_url", sa.String(length=500), nullable=True))
        batch.add_column(sa.Column("sprite_content_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("sprite_generation_run_id", sa.String(length=100), nullable=True))

    op.create_table(
        "resident_sprite_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("resident_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="requested"),
        sa.Column("direction_policy", sa.String(length=32), nullable=False, server_default="mirror_right"),
        sa.Column("generation_request_json", sa.JSON(), nullable=False),
        sa.Column("retry_of_run_id", sa.String(length=100), nullable=True),
        sa.Column("capability_receipt_id", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manifest_path", sa.String(length=1000), nullable=True),
        sa.Column("candidate_texture_path", sa.String(length=1000), nullable=True),
        sa.Column("candidate_portrait_path", sa.String(length=1000), nullable=True),
        sa.Column("candidate_texture_sha256", sa.String(length=64), nullable=True),
        sa.Column("candidate_portrait_sha256", sa.String(length=64), nullable=True),
        sa.Column("published_texture_sha256", sa.String(length=64), nullable=True),
        sa.Column("published_portrait_sha256", sa.String(length=64), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("review_evidence_json", sa.JSON(), nullable=True),
        sa.Column("review_checklist_json", sa.JSON(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("published_by", sa.String(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_by", sa.String(), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        sa.Column("previous_sprite_url", sa.String(length=500), nullable=True),
        sa.Column("previous_portrait_url", sa.String(length=500), nullable=True),
        sa.Column("previous_sprite_content_hash", sa.String(length=64), nullable=True),
        sa.Column("previous_sprite_generation_run_id", sa.String(length=100), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["resident_id"], ["residents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rolled_back_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_resident_sprite_runs_run_id"),
    )
    op.create_index("ix_resident_sprite_runs_resident_id", "resident_sprite_runs", ["resident_id"])
    op.create_index("ix_resident_sprite_runs_run_id", "resident_sprite_runs", ["run_id"])
    op.create_index("ix_resident_sprite_runs_status", "resident_sprite_runs", ["status"])
    op.create_index("ix_resident_sprite_runs_lease_owner", "resident_sprite_runs", ["lease_owner"])
    op.create_index("ix_resident_sprite_runs_retry_of_run_id", "resident_sprite_runs", ["retry_of_run_id"])
    op.create_index("ix_resident_sprite_runs_lease_expires_at", "resident_sprite_runs", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_resident_sprite_runs_retry_of_run_id", table_name="resident_sprite_runs")
    op.drop_index("ix_resident_sprite_runs_lease_expires_at", table_name="resident_sprite_runs")
    op.drop_index("ix_resident_sprite_runs_lease_owner", table_name="resident_sprite_runs")
    op.drop_index("ix_resident_sprite_runs_status", table_name="resident_sprite_runs")
    op.drop_index("ix_resident_sprite_runs_run_id", table_name="resident_sprite_runs")
    op.drop_index("ix_resident_sprite_runs_resident_id", table_name="resident_sprite_runs")
    op.drop_table("resident_sprite_runs")
    with op.batch_alter_table("residents") as batch:
        batch.drop_column("sprite_generation_run_id")
        batch.drop_column("sprite_content_hash")
        batch.drop_column("sprite_url")
