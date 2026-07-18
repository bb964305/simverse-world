"""Lab Agent v1 — Simverse Lab Runtime Protocol v1 contracts and storage
(Grant/Policy/Broker boundary, PRD §Protocols, §Data and API Evolution, P0).
Adds the event ledger + outbox, capability grants, tool actions + approvals,
run lease, run budget ledger, and the world-revision audit trail.

WRITE ONLY — do not run during burn-in. Verify on real Postgres before deploy.
Chains onto 033_add_world_governance.

Revision ID: 034_add_lab_agent_v1
Revises: 033_add_world_governance
Create Date: 2026-07-18

"""
from alembic import op
import sqlalchemy as sa

revision = "034_add_lab_agent_v1"
down_revision = "033_add_world_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lab_run_events",
        sa.Column("event_id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=60), nullable=False),
        sa.Column("action_id", sa.String(), nullable=True),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("provider_event_id", sa.String(), nullable=True),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("policy_version", sa.String(length=20), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_lab_run_events_run_seq"),
        sa.UniqueConstraint("run_id", "provider_event_id", name="uq_lab_run_events_provider"),
    )
    op.create_index("ix_lab_run_events_tenant_id", "lab_run_events", ["tenant_id"])
    op.create_index("ix_lab_run_events_run_id", "lab_run_events", ["run_id"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(length=36), nullable=False, unique=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("topic", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outbox_events_run_id", "outbox_events", ["run_id"])

    op.create_table(
        "lab_capability_grants",
        sa.Column("jti", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(length=60), nullable=False),
        sa.Column("parent_jti", sa.String(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("audience", sa.String(length=30), nullable=False, server_default="tool-broker"),
        sa.Column("capabilities_json", sa.JSON(), nullable=True),
        sa.Column("resources_json", sa.JSON(), nullable=True),
        sa.Column("egress_json", sa.JSON(), nullable=True),
        sa.Column("budgets_json", sa.JSON(), nullable=True),
        sa.Column("policy_version", sa.String(length=20), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("nbf", sa.Integer(), nullable=False),
        sa.Column("exp", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grant_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lab_capability_grants_tenant_id", "lab_capability_grants", ["tenant_id"])
    op.create_index("ix_lab_capability_grants_run_id", "lab_capability_grants", ["run_id"])

    op.create_table(
        "lab_tool_actions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(length=80), nullable=False),
        sa.Column("tool_version", sa.String(length=20), nullable=False, server_default="1"),
        sa.Column("args_hash", sa.String(length=64), nullable=False),
        sa.Column("args_redacted_json", sa.JSON(), nullable=True),
        sa.Column("risk_class", sa.String(length=4), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="requested"),
        sa.Column("grant_jti", sa.String(), nullable=True),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("policy_version", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False, unique=True),
        sa.Column("approval_id", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("artifact_id", sa.String(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lab_tool_actions_tenant_id", "lab_tool_actions", ["tenant_id"])
    op.create_index("ix_lab_tool_actions_run_id", "lab_tool_actions", ["run_id"])
    op.create_index("ix_lab_tool_actions_status", "lab_tool_actions", ["status"])

    op.create_table(
        "lab_approvals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("action_id", sa.String(), nullable=False, unique=True),
        sa.Column("preview_json", sa.JSON(), nullable=True),
        sa.Column("args_digest", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decision_scope", sa.String(length=30), nullable=False, server_default="task_owner"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lab_approvals_tenant_id", "lab_approvals", ["tenant_id"])
    op.create_index("ix_lab_approvals_run_id", "lab_approvals", ["run_id"])
    op.create_index("ix_lab_approvals_decision", "lab_approvals", ["decision"])

    op.create_table(
        "lab_run_leases",
        sa.Column("run_id", sa.String(length=36), primary_key=True),
        sa.Column("owner_id", sa.String(length=80), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "lab_run_budgets",
        sa.Column("run_id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("limit_model_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_model_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_model_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("limit_tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("limit_wall_clock_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_wall_clock_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_wall_clock_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("limit_egress_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_egress_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_egress_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("limit_egress_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_egress_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_egress_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("limit_artifact_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_artifact_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_artifact_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("limit_artifact_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_artifact_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_artifact_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("limit_active_workers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_active_workers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_active_workers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exhausted_dimension", sa.String(length=20), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "world_revisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("proposal_id", sa.String(), nullable=False),
        sa.Column("location_slug", sa.String(length=100), nullable=False),
        sa.Column("change_kind", sa.String(length=30), nullable=False),
        sa.Column("base_revision_id", sa.String(), nullable=True),
        sa.Column("before_state_json", sa.JSON(), nullable=True),
        sa.Column("after_state_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="applied"),
        sa.Column("applied_by", sa.String(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reverted_by", sa.String(), nullable=True),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_world_revisions_proposal_id", "world_revisions", ["proposal_id"])
    op.create_index("ix_world_revisions_location_slug", "world_revisions", ["location_slug"])


def downgrade() -> None:
    op.drop_index("ix_world_revisions_location_slug", table_name="world_revisions")
    op.drop_index("ix_world_revisions_proposal_id", table_name="world_revisions")
    op.drop_table("world_revisions")

    op.drop_table("lab_run_budgets")

    op.drop_table("lab_run_leases")

    op.drop_index("ix_lab_approvals_decision", table_name="lab_approvals")
    op.drop_index("ix_lab_approvals_run_id", table_name="lab_approvals")
    op.drop_index("ix_lab_approvals_tenant_id", table_name="lab_approvals")
    op.drop_table("lab_approvals")

    op.drop_index("ix_lab_tool_actions_status", table_name="lab_tool_actions")
    op.drop_index("ix_lab_tool_actions_run_id", table_name="lab_tool_actions")
    op.drop_index("ix_lab_tool_actions_tenant_id", table_name="lab_tool_actions")
    op.drop_table("lab_tool_actions")

    op.drop_index("ix_lab_capability_grants_run_id", table_name="lab_capability_grants")
    op.drop_index("ix_lab_capability_grants_tenant_id", table_name="lab_capability_grants")
    op.drop_table("lab_capability_grants")

    op.drop_index("ix_outbox_events_run_id", table_name="outbox_events")
    op.drop_table("outbox_events")

    op.drop_index("ix_lab_run_events_run_id", table_name="lab_run_events")
    op.drop_index("ix_lab_run_events_tenant_id", table_name="lab_run_events")
    op.drop_table("lab_run_events")
