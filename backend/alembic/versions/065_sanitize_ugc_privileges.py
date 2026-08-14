"""Remove legacy self-signed privilege metadata from UGC residents.

Revision ID: 065_sanitize_ugc_privileges
Revises: 064_forge_quota_counters
Create Date: 2026-08-14

The public import boundary used to persist uploaded ``meta.json`` verbatim.
Consumer-side checks make those fields inert after this release; this migration
also removes them so stale reputation is not displayed before the next nightly
recompute and operators do not mistake an unsigned flag for an admin grant.
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "065_sanitize_ugc_privileges"
down_revision = "064_forge_quota_counters"
branch_labels = None
depends_on = None

_UGC_ORIGINS = frozenset({"import", "forge", "quick_forge"})
_UNTRUSTED_KEYS = frozenset({
    "_server_privilege_grants",
    "duty",
    "lab",
    "mayor",
    "prompt_hint",
    "reputation",
})


def _as_meta_dict(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _sanitize_ugc_meta(connection) -> int:
    """Portable row-by-row JSON cleanup used by ``upgrade`` and tests."""
    residents = sa.table(
        "residents",
        sa.column("id", sa.String()),
        sa.column("meta_json", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(residents.c.id, residents.c.meta_json).where(
            residents.c.meta_json.is_not(None)
        )
    ).mappings().all()

    changed = 0
    for row in rows:
        meta = _as_meta_dict(row["meta_json"])
        if not meta or meta.get("origin") not in _UGC_ORIGINS:
            continue
        clean = {key: value for key, value in meta.items() if key not in _UNTRUSTED_KEYS}
        if clean == meta:
            continue
        connection.execute(
            sa.update(residents)
            .where(residents.c.id == row["id"])
            .values(meta_json=clean)
        )
        changed += 1
    return changed


def upgrade() -> None:
    # No legitimate HMAC grant can predate this revision: 065 ships the first
    # server grant write path. Therefore even a legacy marker-shaped key is
    # attacker-controlled and must be removed with the unsigned payload.
    _sanitize_ugc_meta(op.get_bind())


def downgrade() -> None:
    # Security cleanup is intentionally irreversible; the removed upload data
    # cannot be distinguished from a legitimate grant after the fact.
    pass
