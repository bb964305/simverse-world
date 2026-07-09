"""Add digests table (A5 village daily report / E14 personal weekly).

Single-table create/drop — verify `upgrade`/`downgrade -1` on real Postgres
before deploy (vm212). Base-week numbering shifted +2, so digests is 019.

Revision ID: 019_add_digests
Revises: 018_add_location_visits
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "019_add_digests"
down_revision = "018_add_location_visits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("stats_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope", "date", "user_id", name="uq_digest_scope_date_user"),
    )
    op.create_index("ix_digests_date", "digests", ["date"])


def downgrade() -> None:
    op.drop_index("ix_digests_date", table_name="digests")
    op.drop_table("digests")
