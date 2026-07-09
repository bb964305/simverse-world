"""Add bulletin_posts table (A4 resident creations / A5 digest / C3 clues).

Single-table create/drop — verify on real Postgres before deploy (vm212).
Chains onto 023_add_resident_goals.

Revision ID: 024_add_bulletin_posts
Revises: 023_add_resident_goals
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "024_add_bulletin_posts"
down_revision = "023_add_resident_goals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bulletin_posts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("author_resident_id", sa.String(), nullable=True),
        sa.Column("author_user_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("likes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tips_sc", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bulletin_posts_author_resident_id", "bulletin_posts", ["author_resident_id"])
    op.create_index("ix_bulletin_posts_kind", "bulletin_posts", ["kind"])
    op.create_index("ix_bulletin_posts_created_at", "bulletin_posts", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_bulletin_posts_created_at", table_name="bulletin_posts")
    op.drop_index("ix_bulletin_posts_kind", table_name="bulletin_posts")
    op.drop_index("ix_bulletin_posts_author_resident_id", table_name="bulletin_posts")
    op.drop_table("bulletin_posts")
