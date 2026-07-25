"""Add town_treasuries table (S1-5 镇财政闭环).

One row per town key ('town' in the single-town MVP): the public account that
sales tax flows into and duty wages / public spending flow out of. Shape mirrors
``resident_treasuries`` (032_add_lab_core) so the proven coin_service atomic
write idioms apply verbatim.

NOTE (收口): the ``NNN`` number is a PLACEHOLDER for this worktree. The measured
chain head at branch time was ``047_add_issue_stances``; the parallel S2-5 line
also chains onto 047, so the main session renumbers these at merge time and
re-verifies ``alembic heads`` is single-headed (KICKOFF §7 链尾单头校验).

Revision ID: NNN_add_town_treasury
Revises: 047_add_issue_stances
Create Date: 2026-07-25
"""
from alembic import op
import sqlalchemy as sa

revision = "NNN_add_town_treasury"
down_revision = "047_add_issue_stances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "town_treasuries",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("balance_sc", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("town_treasuries")
