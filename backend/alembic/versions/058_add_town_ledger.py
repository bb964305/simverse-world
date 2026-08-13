"""Add an append-only ledger for the town treasury.

The scalar balance predates the ledger.  Each existing town row therefore
receives one ``opening_balance`` entry so forward reconciliation starts from a
complete anchor rather than an unexplained delta.

Revision ID: 058_add_town_ledger
Revises: 057_add_arc_template_key
Create Date: 2026-08-11
"""
from datetime import UTC, datetime
import uuid

from alembic import op
import sqlalchemy as sa


revision = "058_add_town_ledger"
down_revision = "057_add_arc_template_key"
branch_labels = None
depends_on = None


_towns = sa.table(
    "town_treasuries",
    sa.column("key", sa.String),
    sa.column("balance_sc", sa.Integer),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
_entries = sa.table(
    "town_treasury_entries",
    sa.column("id", sa.String),
    sa.column("town_key", sa.String),
    sa.column("amount_sc", sa.Integer),
    sa.column("balance_after_sc", sa.Integer),
    sa.column("reason", sa.String),
    sa.column("resident_slug", sa.String),
    sa.column("ref_key", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def _anchor_existing_balances(bind) -> int:
    rows = bind.execute(
        sa.select(_towns.c.key, _towns.c.balance_sc, _towns.c.updated_at)
    ).fetchall()
    if not rows:
        return 0
    now = datetime.now(UTC)
    bind.execute(_entries.insert(), [
        {
            "id": str(uuid.uuid4()),
            "town_key": row.key,
            "amount_sc": int(row.balance_sc or 0),
            "balance_after_sc": int(row.balance_sc or 0),
            "reason": "opening_balance",
            "resident_slug": None,
            "ref_key": f"opening_balance:{row.key}",
            "created_at": row.updated_at or now,
        }
        for row in rows
    ])
    return len(rows)


def upgrade() -> None:
    op.create_table(
        "town_treasury_entries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "town_key", sa.String(length=100),
            sa.ForeignKey("town_treasuries.key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount_sc", sa.Integer(), nullable=False),
        sa.Column("balance_after_sc", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("resident_slug", sa.String(length=100), nullable=True),
        sa.Column("ref_key", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ref_key", name="uq_town_treasury_entries_ref_key"),
    )
    op.create_index(
        "ix_town_treasury_entries_created_at",
        "town_treasury_entries", ["created_at"],
    )
    op.create_index(
        "ix_town_treasury_entries_reason",
        "town_treasury_entries", ["reason"],
    )
    _anchor_existing_balances(op.get_bind())


def downgrade() -> None:
    op.drop_index(
        "ix_town_treasury_entries_reason", table_name="town_treasury_entries"
    )
    op.drop_index(
        "ix_town_treasury_entries_created_at",
        table_name="town_treasury_entries",
    )
    op.drop_table("town_treasury_entries")
