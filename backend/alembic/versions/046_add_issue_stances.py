"""Add issue_stances table (S1-3 议题立场与舆论动力学).

One row per (issue_key, resident_slug): a bounded-confidence stance scalar in
[-1, 1] plus bookkeeping. issue_key is a denormalized free string (no issues
table, no FK) — see KICKOFF_S1-3_opinion.md §2 任务 1.

NOTE (收口): the "046" number is provisional for this worktree. Parallel
kickoff lines each chain onto the current head (045) in their own worktree;
the main session linearizes the numbers at merge time and re-verifies
`alembic heads` single-head (KICKOFF §8.3).

Revision ID: 046_add_issue_stances
Revises: 045_residents_creator_nullable
Create Date: 2026-07-25
"""
from alembic import op
import sqlalchemy as sa

revision = "046_add_issue_stances"
down_revision = "045_residents_creator_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "issue_stances",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("issue_key", sa.String(length=300), nullable=False),
        sa.Column("resident_slug", sa.String(length=100), nullable=False),
        sa.Column("stance", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("updated_from", sa.String(length=16), nullable=True),
        sa.Column("interact_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_update_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("issue_key", "resident_slug", name="uq_issue_stance"),
    )
    op.create_index("ix_issue_stance_issue", "issue_stances", ["issue_key"])
    op.create_index("ix_issue_stance_resident", "issue_stances", ["resident_slug"])


def downgrade() -> None:
    op.drop_index("ix_issue_stance_resident", table_name="issue_stances")
    op.drop_index("ix_issue_stance_issue", table_name="issue_stances")
    op.drop_table("issue_stances")
