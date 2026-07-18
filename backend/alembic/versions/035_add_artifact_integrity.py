"""V12 artifact integrity + retention — DB-slice on the existing LabArtifact
(PRD §Artifacts and World Proposals, §Instruction and Memory Layers retention
section; scope ruling in .superpowers/sdd/task-9-brief.md: no object store,
that is P3). Adds digest/tenant/provenance/scan/verification/retention-hold/
expiry columns, all nullable or defaulted so pre-existing rows and the
flag-off legacy path are unaffected.

Plain ``op.add_column`` — SQLite supports native ADD COLUMN, no batch mode
needed. Downgrade uses ``op.drop_column`` following the 020/021/031
precedent; SQLite cannot drop a column without a full table rebuild
(unsupported here), so downgrade is Postgres-only — verify there before
deploy, per WRITE ONLY note below.

WRITE ONLY — do not run during burn-in. Verify on real Postgres before deploy.
Chains onto 034_add_lab_agent_v1.

Revision ID: 035_add_artifact_integrity
Revises: 034_add_lab_agent_v1
Create Date: 2026-07-18

"""
from alembic import op
import sqlalchemy as sa

revision = "035_add_artifact_integrity"
down_revision = "034_add_lab_agent_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lab_artifacts", sa.Column("tenant_id", sa.String(length=36), nullable=True))
    op.create_index("ix_lab_artifacts_tenant_id", "lab_artifacts", ["tenant_id"])
    op.add_column("lab_artifacts", sa.Column("sha256", sa.String(length=64), nullable=True))
    op.add_column("lab_artifacts", sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("lab_artifacts", sa.Column("producer_action_id", sa.String(length=36), nullable=True))
    op.add_column("lab_artifacts", sa.Column("provenance", sa.String(length=30), nullable=False, server_default="runtime"))
    op.add_column("lab_artifacts", sa.Column("scan_status", sa.String(length=20), nullable=False, server_default="skipped"))
    op.add_column("lab_artifacts", sa.Column("verification_status", sa.String(length=20), nullable=False, server_default="unverified"))
    op.add_column("lab_artifacts", sa.Column("retention_hold", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("lab_artifacts", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("lab_artifacts", "expires_at")
    op.drop_column("lab_artifacts", "retention_hold")
    op.drop_column("lab_artifacts", "verification_status")
    op.drop_column("lab_artifacts", "scan_status")
    op.drop_column("lab_artifacts", "provenance")
    op.drop_column("lab_artifacts", "producer_action_id")
    op.drop_column("lab_artifacts", "byte_size")
    op.drop_column("lab_artifacts", "sha256")
    op.drop_index("ix_lab_artifacts_tenant_id", table_name="lab_artifacts")
    op.drop_column("lab_artifacts", "tenant_id")
