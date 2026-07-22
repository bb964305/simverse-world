"""fence world proposals and enforce one revision per proposal

Revision ID: 042_lab_world_fencing
Revises: 041_lab_control_plane
Create Date: 2026-07-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "042_lab_world_fencing"
down_revision = "041_lab_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "world_change_proposals",
        sa.Column(
            "global_fencing_epoch",
            sa.Integer(),
            nullable=True,
            server_default="0",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE world_change_proposals SET global_fencing_epoch = 0 "
            "WHERE global_fencing_epoch IS NULL"
        )
    )
    op.alter_column(
        "world_change_proposals",
        "global_fencing_epoch",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=None,
    )
    op.create_check_constraint(
        "ck_world_change_proposals_global_epoch",
        "world_change_proposals",
        "global_fencing_epoch >= 0",
    )

    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            "SELECT proposal_id FROM world_revisions "
            "GROUP BY proposal_id HAVING count(*) > 1 LIMIT 1"
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise RuntimeError(
            "cannot enforce one world revision per proposal while duplicates exist"
        )
    op.create_unique_constraint(
        "uq_world_revisions_proposal",
        "world_revisions",
        ["proposal_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_world_revisions_proposal",
        "world_revisions",
        type_="unique",
    )
    op.drop_constraint(
        "ck_world_change_proposals_global_epoch",
        "world_change_proposals",
        type_="check",
    )
    op.drop_column("world_change_proposals", "global_fencing_epoch")
