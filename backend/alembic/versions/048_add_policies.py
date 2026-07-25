"""S2-5 policies — typed/tiered/versioned policy table (KICKOFF_S2-5 §2 任务 1).

New table only (``create_table``; no ALTER on any existing table — the tier
lives on the ``policies`` side, ``world_change_proposals`` gains no column, see
KICKOFF §7). SQLite-safe both ways, so no ``batch_alter_table`` is needed.

Seeding is deliberately NOT done here: ``PolicyService.seed_defaults()`` owns
it as an idempotent dialect-aware upsert, gated behind
``POLIS_POLICY_ENABLED`` (KICKOFF §2 任务 2 / §4). A migration that seeded rows
would populate the table on machines where the feature is off.

NOTE 迁移号占位: the "048" number is provisional for this worktree. Parallel
kickoff lines (S1-5 town_treasury) each chain onto the same verified head
``047_add_issue_stances`` in their own worktree; the main session linearizes
the numbers at merge time and re-verifies single-head (`alembic heads`).

Revision ID: 048_add_policies
Revises: 047_add_issue_stances
Create Date: 2026-07-25
"""
import sqlalchemy as sa
from alembic import op

revision = "048_add_policies"
down_revision = "047_add_issue_stances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("tier", sa.String(length=32), nullable=False),
        sa.Column("procedure", sa.String(length=64), nullable=False),
        sa.Column("group", sa.String(length=50), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("key", name="uq_policies_key"),
    )
    op.create_index("ix_policies_key", "policies", ["key"])
    op.create_index("ix_policies_tier", "policies", ["tier"])
    op.create_index("ix_policies_group", "policies", ["group"])


def downgrade() -> None:
    op.drop_index("ix_policies_group", table_name="policies")
    op.drop_index("ix_policies_tier", table_name="policies")
    op.drop_index("ix_policies_key", table_name="policies")
    op.drop_table("policies")
