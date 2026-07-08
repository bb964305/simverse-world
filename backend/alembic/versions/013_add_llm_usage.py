"""Add llm_usage per-attempt telemetry table (P1-1, E-19/E-23).

Append-only, **no foreign keys**: this is a high-write telemetry table whose
rows must outlive the resident/user/conversation they reference and must never
couple the business transaction. Plain indexed id columns (not FKs) also avoid
the FK-insert-order / type-drift class of bug that only surfaces on real
Postgres (vm212). Single-table create/drop — verify `upgrade`/`downgrade -1`
on real Postgres before deploy.

Revision ID: 013_add_llm_usage
Revises: 012_sync_schema_drift
Create Date: 2026-07-08

"""
from alembic import op
import sqlalchemy as sa

revision = "013_add_llm_usage"
down_revision = "012_sync_schema_drift"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scenario", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("owner", sa.String(length=16), nullable=False),
        sa.Column("resident_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("parse_ok", sa.Boolean(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_creation_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="usage"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_llm_usage_ts", "llm_usage", ["ts"])
    op.create_index("ix_llm_usage_scenario", "llm_usage", ["scenario"])
    op.create_index("ix_llm_usage_resident_id", "llm_usage", ["resident_id"])
    op.create_index("ix_llm_usage_user_id", "llm_usage", ["user_id"])
    op.create_index("ix_llm_usage_conversation_id", "llm_usage", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_conversation_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_user_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_resident_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_scenario", table_name="llm_usage")
    op.drop_index("ix_llm_usage_ts", table_name="llm_usage")
    op.drop_table("llm_usage")
