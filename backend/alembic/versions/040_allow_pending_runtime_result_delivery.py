"""allow a Runtime result to be durable before transport delivery

Revision ID: 040_runtime_result_delivery
Revises: 039_add_lab_protocol_v2_state
Create Date: 2026-07-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "040_runtime_result_delivery"
down_revision = "039_add_lab_protocol_v2_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lab_runtime_sessions",
        sa.Column("authority_epoch", sa.Integer(), nullable=True),
    )
    op.execute(sa.text(
        "UPDATE lab_runtime_sessions SET authority_epoch = fencing_epoch "
        "WHERE authority_epoch IS NULL"
    ))
    op.alter_column(
        "lab_runtime_sessions",
        "authority_epoch",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_lab_runtime_sessions_authority_epoch",
        "lab_runtime_sessions",
        "authority_epoch >= fencing_epoch",
    )
    op.alter_column(
        "lab_runtime_results",
        "receipt_id",
        existing_type=sa.String(length=100),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_lab_runtime_results_receipt_ack_pair",
        "lab_runtime_results",
        "(receipt_id IS NULL AND runtime_acked_at IS NULL) OR "
        "(receipt_id IS NOT NULL AND runtime_acked_at IS NOT NULL)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    pending = bind.execute(sa.text(
        "SELECT count(*) FROM lab_runtime_results WHERE receipt_id IS NULL"
    )).scalar_one()
    if pending:
        raise RuntimeError(
            "cannot downgrade while Runtime results are pending delivery"
        )
    op.drop_constraint(
        "ck_lab_runtime_results_receipt_ack_pair",
        "lab_runtime_results",
        type_="check",
    )
    op.drop_constraint(
        "ck_lab_runtime_sessions_authority_epoch",
        "lab_runtime_sessions",
        type_="check",
    )
    op.drop_column("lab_runtime_sessions", "authority_epoch")
    op.alter_column(
        "lab_runtime_results",
        "receipt_id",
        existing_type=sa.String(length=100),
        nullable=False,
    )
