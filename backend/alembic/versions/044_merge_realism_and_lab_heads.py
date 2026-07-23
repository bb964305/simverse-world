"""Merge the realism and lab protocol-v2 migration heads.

The realism chain (038_add_realism_fields -> 039_add_resident_relations) and
the lab protocol-v2 chain (038_add_lab_terminalization_v2 -> ... ->
043_lab_artifact_pipeline) both branched off 037_add_lab_worker_attempts on
separate feature branches. This no-op merge revision joins the two heads so
``alembic upgrade head`` resolves to a single head again. A database that has
only applied one branch will apply the other branch's revisions on upgrade.

Revision ID: 044_merge_realism_lab_heads
Revises: 043_lab_artifact_pipeline, 039_add_resident_relations
Create Date: 2026-07-23

"""

revision = "044_merge_realism_lab_heads"
down_revision = ("043_lab_artifact_pipeline", "039_add_resident_relations")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
