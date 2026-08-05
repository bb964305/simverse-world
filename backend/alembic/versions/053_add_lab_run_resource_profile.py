"""Add immutable compute resource profiles to Lab runs.

Revision ID: 053_add_lab_run_resource_profile
Revises: 052_add_lab_codex_model_tier
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa


revision = "053_add_lab_run_resource_profile"
down_revision = "052_add_lab_codex_model_tier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("lab_runs") as batch:
        batch.add_column(sa.Column(
            "resource_cpu_cores", sa.Integer(), nullable=False, server_default="2"
        ))
        batch.add_column(sa.Column(
            "resource_memory_mb", sa.Integer(), nullable=False, server_default="2048"
        ))
        batch.create_check_constraint(
            "ck_lab_runs_resource_profile",
            "(model_tier = 'low' AND resource_cpu_cores = 2 AND resource_memory_mb = 2048) OR "
            "(model_tier = 'high' AND resource_cpu_cores = 4 AND resource_memory_mb = 4096)",
        )


def downgrade() -> None:
    with op.batch_alter_table("lab_runs") as batch:
        batch.drop_constraint("ck_lab_runs_resource_profile", type_="check")
        batch.drop_column("resource_memory_mb")
        batch.drop_column("resource_cpu_cores")
