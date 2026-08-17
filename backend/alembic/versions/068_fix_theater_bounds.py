"""Move the voted theater into the walkable band.

Revision ID: 068_fix_theater_bounds
Revises: 067_market_economy_loop
Create Date: 2026-08-17

WALKABLE_X_RANGE tops out at x=173 (world_geometry.py:9) while the theater was
built with bounds x2=178 and center x=175. The center is force-marked walkable
by pathfinder._get_forced_walkable but is NOT in the hub-connected component,
so find_path to it returns None. It is harmless today only because
map_data.get_valid_target_tile prefers ``entrance`` and never falls back to
``center`` when one exists — deleting that entrance would strand the building.

The entrance (172,45) is measured reachable and stays put; it still lies inside
the new bounds, which apply.validate_location_patch requires.

Data-only, idempotent, and deliberately shipped in its own batch: no code
behaviour changes and no gate is flipped here.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "068_fix_theater_bounds"
down_revision = "067_market_economy_loop"
branch_labels = None
depends_on = None

_OLD = {"bounds": [172, 40, 178, 50], "center": [175, 45]}
_NEW = {"bounds": [168, 40, 173, 50], "center": [170, 45]}

_rows = sa.table(
    "dynamic_locations",
    sa.column("id", sa.String),
    sa.column("slug", sa.String),
    sa.column("data_json", sa.JSON),
)


def _rewrite(connection, new: dict, old: dict) -> int:
    """Portable row rewrite used by upgrade/downgrade and by the tests.

    Returns the number of rows changed. Only touches a row whose coordinates
    still match ``old`` exactly, so a hand-edited production row is left alone
    and a re-run is a no-op. Uses Core constructs (never a raw json string) so
    the sa.JSON column round-trips on both Postgres and sqlite.
    """
    row = connection.execute(
        sa.select(_rows.c.id, _rows.c.data_json).where(_rows.c.slug == "theater")
    ).fetchone()
    if row is None:
        return 0
    data = dict(row[1] or {})
    try:
        current = [int(v) for v in (data.get("bounds") or [])]
    except (TypeError, ValueError):
        return 0
    if current != old["bounds"]:
        return 0
    data["bounds"] = list(new["bounds"])
    data["center"] = list(new["center"])
    connection.execute(
        _rows.update().where(_rows.c.id == row[0]).values(data_json=data)
    )
    return 1


def upgrade() -> None:
    _rewrite(op.get_bind(), _NEW, _OLD)


def downgrade() -> None:
    _rewrite(op.get_bind(), _OLD, _NEW)
