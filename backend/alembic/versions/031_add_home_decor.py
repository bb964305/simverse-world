"""B3 home decor: residents.home_decor_json.

JSON list column ``[{item_code, x, y, rot}]`` — x/y are tile offsets relative
to the resident's home_location_id bbox (app/agent/map_data.py bounds).
Nullable; NULL means "never decorated". Spec numbered this 021 but that slot
is taken by 021_add_daily_loop, so it chains onto 030 as 031.

Revision ID: 031_add_home_decor
Revises: 030_add_perf_indexes
Create Date: 2026-07-13

"""
import sqlalchemy as sa
from alembic import op

revision = "031_add_home_decor"
down_revision = "030_add_perf_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("residents", sa.Column("home_decor_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("residents", "home_decor_json")
