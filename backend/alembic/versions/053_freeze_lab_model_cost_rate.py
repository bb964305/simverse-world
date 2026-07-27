"""Freeze the USD-to-SC model-cost rate on each Lab run.

Revision ID: 053_freeze_lab_model_cost_rate
Revises: 052_add_lab_run_resource_profile
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa


revision = "053_freeze_lab_model_cost_rate"
down_revision = "052_add_lab_run_resource_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("lab_runs") as batch:
        batch.add_column(sa.Column(
            "model_cost_sc_per_usd",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ))
        batch.create_check_constraint(
            "ck_lab_runs_model_cost_sc_per_usd_positive",
            "model_cost_sc_per_usd > 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("lab_runs") as batch:
        batch.drop_constraint(
            "ck_lab_runs_model_cost_sc_per_usd_positive", type_="check"
        )
        batch.drop_column("model_cost_sc_per_usd")
