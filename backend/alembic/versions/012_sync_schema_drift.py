"""Sync schema drift accumulated while create_all masked the migration chain.

Three drifts surfaced by the first real `alembic upgrade head` run on
PostgreSQL (vm212, 2026-07-07):
- residents.versions_json existed in the ORM but no migration created it;
  movement_path_json/movement_target_json (010) were later removed from the
  ORM but never dropped.
- forge_sessions was rewritten for the pipeline architecture
  (research/extraction/build/validation/refinement columns); the legacy
  Text columns from 003 are dead (legacy forge_service keeps its state in
  an in-memory dict, nothing reads them).
- forge_sessions.current_stage was Integer in 003 but the ORM uses String.

Revision ID: 012_sync_schema_drift
Revises: 011_backfill_home
Create Date: 2026-07-07

"""
from alembic import op
import sqlalchemy as sa

revision = "012_sync_schema_drift"
down_revision = "011_backfill_home"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # residents
    op.add_column("residents", sa.Column("versions_json", sa.JSON(), nullable=True))
    op.drop_column("residents", "movement_target_json")
    op.drop_column("residents", "movement_path_json")

    # forge_sessions — pipeline columns
    for col in ("research_data", "extraction_data", "build_output",
                "validation_report", "refinement_log"):
        op.add_column("forge_sessions", sa.Column(col, sa.JSON(), nullable=True))

    # forge_sessions — dead legacy columns from 003
    for col in ("answers_json", "ability_json", "persona_json",
                "soul_json", "meta_json"):
        op.drop_column("forge_sessions", col)

    op.alter_column(
        "forge_sessions", "current_stage",
        type_=sa.String(length=50),
        existing_type=sa.Integer(),
        server_default="",
        postgresql_using="current_stage::varchar",
    )


def downgrade() -> None:
    # Drop the varchar default first: PG cannot auto-cast it during the
    # type change back to integer.
    op.alter_column("forge_sessions", "current_stage",
                    server_default=None, existing_type=sa.String(length=50))
    op.alter_column(
        "forge_sessions", "current_stage",
        type_=sa.Integer(),
        existing_type=sa.String(length=50),
        postgresql_using="NULLIF(current_stage, '')::integer",
    )
    op.alter_column("forge_sessions", "current_stage",
                    server_default="1", existing_type=sa.Integer())

    for col in ("meta_json", "soul_json", "persona_json",
                "ability_json", "answers_json"):
        op.add_column("forge_sessions", sa.Column(col, sa.Text(), nullable=True))

    for col in ("refinement_log", "validation_report", "build_output",
                "extraction_data", "research_data"):
        op.drop_column("forge_sessions", col)

    op.add_column("residents", sa.Column("movement_path_json", sa.JSON(), nullable=True))
    op.add_column("residents", sa.Column("movement_target_json", sa.JSON(), nullable=True))
    op.drop_column("residents", "versions_json")
