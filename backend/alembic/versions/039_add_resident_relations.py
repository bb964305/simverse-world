"""Numeric two-axis relationship table (P2 §7.1 — resident_relations).

One row per canonical undirected party pair (familiarity + affinity axes),
unifying resident-resident and resident-player ties. Unique (party_a, party_b)
enforces the single-row-per-pair invariant; the party_b index covers "all
relations of X" when X is the larger id.

New table only — no ALTER — so it is SQLite-safe both ways.

WRITE ONLY — do not run during burn-in. Verify on real Postgres before deploy.
Chains onto 038_add_realism_fields.

Revision ID: 039_add_resident_relations
Revises: 038_add_realism_fields
Create Date: 2026-07-22

"""
from alembic import op
import sqlalchemy as sa

revision = "039_add_resident_relations"
down_revision = "038_add_realism_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resident_relations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("party_a", sa.String(), nullable=False),
        sa.Column("party_a_type", sa.String(length=20), nullable=False, server_default="resident"),
        sa.Column("party_b", sa.String(), nullable=False),
        sa.Column("party_b_type", sa.String(length=20), nullable=False, server_default="resident"),
        sa.Column("familiarity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("affinity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("interact_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_interact_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("party_a", "party_b", name="uq_resident_relation_pair"),
    )
    op.create_index("ix_resident_relation_party_b", "resident_relations", ["party_b"])


def downgrade() -> None:
    op.drop_index("ix_resident_relation_party_b", table_name="resident_relations")
    op.drop_table("resident_relations")
