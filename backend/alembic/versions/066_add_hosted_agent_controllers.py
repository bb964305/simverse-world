"""Add durable hosted Agent controllers, turn journals and daily budgets.

Revision ID: 066_hosted_agent_controllers
Revises: 065_sanitize_ugc_privileges
Create Date: 2026-08-15
"""

from alembic import op
import sqlalchemy as sa


revision = "066_hosted_agent_controllers"
down_revision = "065_sanitize_ugc_privileges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hosted_agent_controllers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("create_request_hash", sa.String(length=64), nullable=False),
        sa.Column("agent_player_id", sa.String(), nullable=True),
        sa.Column("desired_status", sa.String(length=16), nullable=False),
        sa.Column("runtime_status", sa.String(length=24), nullable=False),
        sa.Column("control_version", sa.Integer(), nullable=False),
        sa.Column("capacity_slot", sa.Integer(), nullable=True),
        sa.Column("provider_host", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("provider_validation_required", sa.Boolean(), nullable=False),
        sa.Column("secret_version", sa.Integer(), nullable=False),
        sa.Column("secret_envelope", sa.Text(), nullable=False),
        sa.Column("identity_json", sa.JSON(), nullable=False),
        sa.Column("policy_json", sa.JSON(), nullable=False),
        sa.Column("heartbeat_seconds", sa.Integer(), nullable=False),
        sa.Column("action_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("max_actions_per_day", sa.Integer(), nullable=False),
        sa.Column("max_provider_calls_per_day", sa.Integer(), nullable=False),
        sa.Column("max_tokens_per_day", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("next_tick_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_presence_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("turn_sequence", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "desired_status IN ('running','paused','disabled')",
            name="ck_hosted_agent_desired_status",
        ),
        sa.CheckConstraint(
            "runtime_status IN ('provisioning','idle','claimed','backoff',"
            "'budget_paused','auth_blocked','error','disabled')",
            name="ck_hosted_agent_runtime_status",
        ),
        sa.CheckConstraint("control_version >= 1", name="ck_hosted_agent_control_version"),
        sa.CheckConstraint("lease_epoch >= 0", name="ck_hosted_agent_lease_epoch"),
        sa.CheckConstraint("turn_sequence >= 0", name="ck_hosted_agent_turn_sequence"),
        sa.CheckConstraint(
            "capacity_slot IS NULL OR capacity_slot >= 0",
            name="ck_hosted_agent_capacity_slot",
        ),
        sa.CheckConstraint(
            "heartbeat_seconds >= 15 AND heartbeat_seconds <= 60",
            name="ck_hosted_agent_heartbeat_seconds",
        ),
        sa.CheckConstraint(
            "action_interval_seconds >= 5 AND action_interval_seconds <= 3600",
            name="ck_hosted_agent_action_interval_seconds",
        ),
        sa.CheckConstraint(
            "max_actions_per_day >= 1 AND max_actions_per_day <= 1000",
            name="ck_hosted_agent_daily_actions",
        ),
        sa.CheckConstraint(
            "max_provider_calls_per_day >= 1 AND max_provider_calls_per_day <= 2000",
            name="ck_hosted_agent_daily_calls",
        ),
        sa.CheckConstraint(
            "max_tokens_per_day >= 1000 AND max_tokens_per_day <= 10000000",
            name="ck_hosted_agent_daily_tokens",
        ),
        sa.CheckConstraint(
            "max_output_tokens >= 1 AND max_output_tokens <= 2000",
            name="ck_hosted_agent_output_tokens",
        ),
        sa.ForeignKeyConstraint(["agent_player_id"], ["agent_players.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_player_id"),
        sa.UniqueConstraint("capacity_slot", name="uq_hosted_agent_capacity_slot"),
        sa.UniqueConstraint(
            "owner_user_id", "request_id", name="uq_hosted_agent_owner_request"
        ),
    )
    op.create_index("ix_hosted_agent_controllers_owner_user_id", "hosted_agent_controllers", ["owner_user_id"])
    op.create_index("ix_hosted_agent_controllers_agent_player_id", "hosted_agent_controllers", ["agent_player_id"])
    op.create_index("ix_hosted_agent_controllers_desired_status", "hosted_agent_controllers", ["desired_status"])
    op.create_index("ix_hosted_agent_controllers_runtime_status", "hosted_agent_controllers", ["runtime_status"])
    op.create_index("ix_hosted_agent_controllers_next_tick_at", "hosted_agent_controllers", ["next_tick_at"])
    op.create_index("ix_hosted_agent_controllers_next_action_at", "hosted_agent_controllers", ["next_action_at"])
    op.create_index("ix_hosted_agent_controllers_lease_owner", "hosted_agent_controllers", ["lease_owner"])
    op.create_index("ix_hosted_agent_controllers_lease_expires_at", "hosted_agent_controllers", ["lease_expires_at"])

    op.create_table(
        "hosted_agent_turns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("controller_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("control_version", sa.Integer(), nullable=False),
        sa.Column("observation_seq", sa.Integer(), nullable=True),
        sa.Column("event_cursor", sa.Integer(), nullable=True),
        sa.Column("observation_version", sa.Integer(), nullable=False),
        sa.Column("observation_envelope", sa.Text(), nullable=False),
        sa.Column("decision_version", sa.Integer(), nullable=True),
        sa.Column("decision_envelope", sa.Text(), nullable=True),
        sa.Column("result_version", sa.Integer(), nullable=True),
        sa.Column("result_envelope", sa.Text(), nullable=True),
        sa.Column("action_id", sa.String(length=64), nullable=True),
        sa.Column("action_type", sa.String(length=32), nullable=True),
        sa.Column("public_summary", sa.String(length=280), nullable=True),
        sa.Column("provider_request_id", sa.String(length=200), nullable=True),
        sa.Column("budget_date", sa.Date(), nullable=True),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("provider_calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("journaled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('observed','budget_reserved','calling','decision_ready',"
            "'committing','completed','abandoned','failed')",
            name="ck_hosted_agent_turn_state",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_hosted_agent_turn_sequence_positive"),
        sa.CheckConstraint("lease_epoch >= 0", name="ck_hosted_agent_turn_lease_epoch"),
        sa.CheckConstraint("control_version >= 1", name="ck_hosted_agent_turn_control_version"),
        sa.CheckConstraint("reserved_tokens >= 0", name="ck_hosted_agent_turn_reserved_tokens"),
        sa.ForeignKeyConstraint(
            ["controller_id"], ["hosted_agent_controllers.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("controller_id", "action_id", name="uq_hosted_agent_turn_action"),
        sa.UniqueConstraint("controller_id", "sequence", name="uq_hosted_agent_turn_sequence"),
    )
    op.create_index("ix_hosted_agent_turns_controller_id", "hosted_agent_turns", ["controller_id"])
    op.create_index("ix_hosted_agent_turns_state", "hosted_agent_turns", ["state"])

    op.create_table(
        "hosted_agent_daily_usage",
        sa.Column("controller_id", sa.String(length=36), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("calls_reserved", sa.Integer(), nullable=False),
        sa.Column("calls_charged", sa.Integer(), nullable=False),
        sa.Column("actions", sa.Integer(), nullable=False),
        sa.Column("tokens_reserved", sa.Integer(), nullable=False),
        sa.Column("tokens_charged", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "calls_reserved >= 0 AND calls_charged >= 0 AND actions >= 0 AND "
            "tokens_reserved >= 0 AND tokens_charged >= 0 AND input_tokens >= 0 "
            "AND output_tokens >= 0",
            name="ck_hosted_agent_usage_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["controller_id"], ["hosted_agent_controllers.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("controller_id", "usage_date"),
    )


def downgrade() -> None:
    op.drop_table("hosted_agent_daily_usage")
    op.drop_index("ix_hosted_agent_turns_state", table_name="hosted_agent_turns")
    op.drop_index("ix_hosted_agent_turns_controller_id", table_name="hosted_agent_turns")
    op.drop_table("hosted_agent_turns")
    op.drop_index("ix_hosted_agent_controllers_lease_expires_at", table_name="hosted_agent_controllers")
    op.drop_index("ix_hosted_agent_controllers_lease_owner", table_name="hosted_agent_controllers")
    op.drop_index("ix_hosted_agent_controllers_next_tick_at", table_name="hosted_agent_controllers")
    op.drop_index("ix_hosted_agent_controllers_next_action_at", table_name="hosted_agent_controllers")
    op.drop_index("ix_hosted_agent_controllers_runtime_status", table_name="hosted_agent_controllers")
    op.drop_index("ix_hosted_agent_controllers_desired_status", table_name="hosted_agent_controllers")
    op.drop_index("ix_hosted_agent_controllers_agent_player_id", table_name="hosted_agent_controllers")
    op.drop_index("ix_hosted_agent_controllers_owner_user_id", table_name="hosted_agent_controllers")
    op.drop_table("hosted_agent_controllers")
