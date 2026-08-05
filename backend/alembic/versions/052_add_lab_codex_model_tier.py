"""Add immutable model-routing snapshot fields to Lab runs.

Integration note (resolved 2026-08-05): this revision originally took the 051
prefix and descended from 050 in parallel with 051_add_civic_standing_history,
which would fork the graph on merge. The lab chain has been re-chained onto the
civic revision and renumbered as a whole: 051/052/053 -> 052/053/054.

Revision ID: 052_add_lab_codex_model_tier
Revises: 051_add_civic_standing_history
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa


revision = "052_add_lab_codex_model_tier"
down_revision = "051_add_civic_standing_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("lab_runs") as batch:
        batch.add_column(sa.Column(
            "model_tier", sa.String(length=10), nullable=False, server_default="low"
        ))
        batch.add_column(sa.Column(
            "model_name", sa.String(length=64), nullable=False,
            server_default="deepseek-v4-flash",
        ))
        batch.add_column(sa.Column(
            "model_policy_version", sa.String(length=64), nullable=False,
            server_default="lab-deepseek-v1",
        ))
        batch.create_check_constraint(
            "ck_lab_runs_model_tier", "model_tier IN ('low','high')"
        )
        batch.create_check_constraint(
            "ck_lab_runs_model_name",
            "model_name IN ('deepseek-v4-flash','deepseek-v4-pro')",
        )
        batch.create_check_constraint(
            "ck_lab_runs_model_tier_name",
            "(model_tier = 'low' AND model_name = 'deepseek-v4-flash') OR "
            "(model_tier = 'high' AND model_name = 'deepseek-v4-pro')",
        )


def downgrade() -> None:
    with op.batch_alter_table("lab_runs") as batch:
        batch.drop_constraint("ck_lab_runs_model_tier_name", type_="check")
        batch.drop_constraint("ck_lab_runs_model_name", type_="check")
        batch.drop_constraint("ck_lab_runs_model_tier", type_="check")
        batch.drop_column("model_policy_version")
        batch.drop_column("model_name")
        batch.drop_column("model_tier")
