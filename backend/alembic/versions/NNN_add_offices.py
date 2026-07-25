"""S2-1 offices — unified job/office table + seed/backfill of the four slots.

New table only (``create_table``, no ALTER — SQLite-safe both ways). Seeds
one row per office (mayor / town_clerk / postman / doctor) and backfills
holders from the current representations:

- mayor  ← system_config['current_mayor'] (the read-path authority);
- town_clerk / postman ← first NPC whose meta_json.duty.key matches
  (find_duty_resident's first-match semantics, done in Python because JSON
  operators are not portable between sqlite and Postgres);
- doctor ← NULL (greenfield: no duty / preset / clinic exists — S5-8).

The backfill NEVER touches meta_json['mayor'] — that flag is the wage-bonus
multiplier consumed by duty_service._pay_wage, not an identity source
(reader gotcha #1). Both stores stay alive; install_mayor dual-writes.

Idempotent: seed skips existing office_key rows; backfill only fills rows
whose holder_slug is still NULL. Tolerates an empty world.

NOTE 迁移号占位: revision id / down_revision use the NNN placeholder per the
multi-branch KICKOFF discipline (S2-5/S1-3/S1-5 all branch off the same
head). down_revision targets the current verified head
``045_residents_creator_nullable``; the merge coordinator linearizes and
renumbers at collect time (`alembic heads` must stay single-headed).

Revision ID: NNN_add_offices
Revises: 045_residents_creator_nullable
Create Date: 2026-07-25
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "NNN_add_offices"
down_revision = "045_residents_creator_nullable"
branch_labels = None
depends_on = None

# (office_key, institution, fill_strategy) — the four S2-1 slots.
OFFICE_SEED = (
    ("mayor", "town_hall", "election"),
    ("town_clerk", "town_hall", "seed"),
    ("postman", "post_office", "seed"),
    ("doctor", "clinic", "appointment"),
)


def _load_json(raw):
    """meta_json / config value as dict-or-value: sqlite raw SQL returns TEXT,
    asyncpg/psycopg may return parsed structures already."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def seed_offices(bind) -> int:
    """Insert missing office rows (idempotent). Returns rows inserted."""
    inserted = 0
    for key, institution, strategy in OFFICE_SEED:
        exists = bind.execute(
            sa.text("SELECT 1 FROM offices WHERE office_key = :k"), {"k": key}
        ).first()
        if exists:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO offices (office_key, holder_slug, institution, "
                "perms_json, fill_strategy, term_started_at, term_ends_at, "
                "created_at, updated_at) "
                "VALUES (:k, NULL, :i, :p, :f, NULL, NULL, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"k": key, "i": institution, "p": json.dumps({}), "f": strategy},
        )
        inserted += 1
    return inserted


def backfill_holders(bind) -> int:
    """Fill holder_slug on still-vacant office rows from today's stores
    (idempotent: only rows with holder_slug IS NULL are touched). Returns
    rows updated. Tolerates an empty world / missing source tables' rows."""
    updated = 0

    # mayor ← system_config['current_mayor'] (values are JSON-encoded)
    row = bind.execute(
        sa.text("SELECT value FROM system_config WHERE key = 'current_mayor'")
    ).first()
    mayor_slug = _load_json(row[0]) if row else None
    if isinstance(mayor_slug, str) and mayor_slug:
        res = bind.execute(
            sa.text(
                "UPDATE offices SET holder_slug = :s, "
                "term_started_at = CURRENT_TIMESTAMP "
                "WHERE office_key = 'mayor' AND holder_slug IS NULL"
            ),
            {"s": mayor_slug},
        )
        updated += res.rowcount or 0

    # town_clerk / postman ← first NPC with meta_json.duty.key == office_key
    # (linear scan in Python — JSON operators are not sqlite/PG portable).
    duty_holders: dict[str, str] = {}
    rows = bind.execute(
        sa.text(
            "SELECT slug, meta_json FROM residents "
            "WHERE resident_type = 'npc' AND meta_json IS NOT NULL "
            "ORDER BY created_at"
        )
    ).fetchall()
    for slug, raw_meta in rows:
        meta = _load_json(raw_meta) or {}
        key = ((meta.get("duty") or {}).get("key"))
        if key in ("town_clerk", "postman") and key not in duty_holders:
            duty_holders[key] = slug
    for key, slug in duty_holders.items():
        res = bind.execute(
            sa.text(
                "UPDATE offices SET holder_slug = :s, "
                "term_started_at = CURRENT_TIMESTAMP "
                "WHERE office_key = :k AND holder_slug IS NULL"
            ),
            {"s": slug, "k": key},
        )
        updated += res.rowcount or 0

    # doctor: greenfield — stays NULL on purpose (S5-8 appoints at runtime).
    return updated


def upgrade() -> None:
    op.create_table(
        "offices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("office_key", sa.String(length=50), nullable=False),
        sa.Column("holder_slug", sa.String(length=100), nullable=True),
        sa.Column("institution", sa.String(length=50), nullable=False),
        sa.Column("perms_json", sa.JSON(), nullable=True),
        sa.Column("fill_strategy", sa.String(length=20), nullable=False),
        sa.Column("term_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("term_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("office_key", name="uq_offices_office_key"),
    )
    bind = op.get_bind()
    seed_offices(bind)
    backfill_holders(bind)


def downgrade() -> None:
    op.drop_table("offices")
