"""Make residents.creator_id nullable so account deletion can orphan NPCs.

DELETE /settings/account promises "owned residents become orphaned NPCs"
(creator_id = NULL), but the initial schema created the column NOT NULL, so
the orphaning UPDATE — and with it the whole account deletion — 500'd on
Postgres (P1 fix, 2026-07-23 production test round).

Revision ID: 045_residents_creator_nullable
Revises: 044_merge_realism_lab_heads

Re-parented from the realism-p2 line's 040 onto the deployed realism+lab-v2
merge head (044) so it forward-applies on production without touching the
lab-v2 041-044 chain.
"""
import sqlalchemy as sa
from alembic import op

revision = "045_residents_creator_nullable"
down_revision = "044_merge_realism_lab_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("residents") as batch_op:
        batch_op.alter_column(
            "creator_id", existing_type=sa.String(), nullable=True
        )


def downgrade() -> None:
    # NULL creator_ids cannot be restored; backfill would be required first.
    with op.batch_alter_table("residents") as batch_op:
        batch_op.alter_column(
            "creator_id", existing_type=sa.String(), nullable=False
        )
