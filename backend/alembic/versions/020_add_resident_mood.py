"""Add residents.mood_json column (E1 emotion engine).

Single-column add/drop — verify on real Postgres before deploy (vm212).
Chains onto 019_add_digests (base-week numbering shifted +2).

Revision ID: 020_add_resident_mood
Revises: 019_add_digests
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "020_add_resident_mood"
down_revision = "019_add_digests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("residents", sa.Column("mood_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("residents", "mood_json")
