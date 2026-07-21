"""Add immutable Lab protocol versions and durable runtime-v2 Gateway state.

Revision ID: 039_add_lab_protocol_v2_state
Revises: 038_add_lab_terminalization_v2
Create Date: 2026-07-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "039_add_lab_protocol_v2_state"
down_revision = "038_add_lab_terminalization_v2"
branch_labels = None
depends_on = None


PROTOCOL_TRIGGER_FUNCTION = r"""
CREATE OR REPLACE FUNCTION public.guard_lab_run_protocol_version()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    IF NEW.protocol_version IS DISTINCT FROM OLD.protocol_version THEN
        RAISE EXCEPTION 'LabRun protocol_version is immutable after creation'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;
"""


def _backfill_enqueue_protocol(bind) -> None:
    """Preserve every payload field while making historical enqueue explicit."""
    outbox = sa.table(
        "outbox_events",
        sa.column("id", sa.Integer()),
        sa.column("run_id", sa.String()),
        sa.column("topic", sa.String()),
        sa.column("payload_json", sa.JSON()),
    )
    runs = sa.table("lab_runs", sa.column("id", sa.String()))
    rows = bind.execute(
        sa.select(
            outbox.c.id,
            outbox.c.run_id,
            outbox.c.payload_json,
            runs.c.id.label("known_run_id"),
        )
        .select_from(outbox.outerjoin(runs, outbox.c.run_id == runs.c.id))
        .where(outbox.c.topic == "lab.run.enqueue")
    ).mappings()
    for row in rows:
        payload = row["payload_json"]
        if not isinstance(payload, dict):
            raise RuntimeError(
                "refusing protocol-v2 migration: lab.run.enqueue payload is not an object"
            )
        envelope_run_id = row["run_id"]
        payload_run_id = payload.get("run_id")
        if (
            not isinstance(envelope_run_id, str)
            or not envelope_run_id
            or not isinstance(payload_run_id, str)
            or payload_run_id != envelope_run_id
            or row["known_run_id"] is None
        ):
            raise RuntimeError(
                "refusing protocol-v2 migration: enqueue run binding is invalid"
            )
        if "protocol_version" in payload:
            version = payload["protocol_version"]
            if type(version) is not int or version != 1:
                raise RuntimeError(
                    "refusing protocol-v2 migration: historical enqueue must be v1"
                )
            continue
        updated = dict(payload)
        updated["protocol_version"] = 1
        bind.execute(
            outbox.update()
            .where(outbox.c.id == row["id"])
            .values(payload_json=updated)
        )


def _tighten_protocol_column(dialect: str) -> None:
    if dialect == "postgresql":
        op.alter_column(
            "lab_runs",
            "protocol_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=None,
        )
        op.create_check_constraint(
            "ck_lab_runs_protocol_version",
            "lab_runs",
            "protocol_version IN (1, 2)",
        )
        return
    with op.batch_alter_table("lab_runs") as batch:
        batch.alter_column(
            "protocol_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=None,
        )
        batch.create_check_constraint(
            "ck_lab_runs_protocol_version", "protocol_version IN (1, 2)"
        )


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.add_column(
        "lab_runs",
        sa.Column(
            "protocol_version",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("1"),
        ),
    )
    bind.execute(
        sa.text("UPDATE lab_runs SET protocol_version = 1 WHERE protocol_version IS NULL")
    )
    _backfill_enqueue_protocol(bind)
    _tighten_protocol_column(dialect)

    op.create_table(
        "lab_runtime_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("lab_runs.id"), nullable=False),
        sa.Column("client_run_id", sa.String(length=80), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("provider_name", sa.String(length=80), nullable=False),
        sa.Column("provider_session_id", sa.String(length=200), nullable=True),
        sa.Column("locator_json", sa.JSON(), nullable=True),
        sa.Column("durability_class", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("creation_owner", sa.String(length=36), nullable=True),
        sa.Column(
            "creation_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "provider_cursor_committed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "provider_cursor_acked", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", name="uq_lab_runtime_sessions_run"),
        sa.UniqueConstraint(
            "client_run_id",
            "fencing_epoch",
            name="uq_lab_runtime_sessions_client_epoch",
        ),
        sa.UniqueConstraint(
            "id", "fencing_epoch", name="uq_lab_runtime_sessions_id_epoch"
        ),
        sa.CheckConstraint(
            "protocol_version = 2", name="ck_lab_runtime_sessions_protocol"
        ),
        sa.CheckConstraint(
            "status IN ('creating','ready','completed','failed','fenced',"
            "'cancelled','quarantined')",
            name="ck_lab_runtime_sessions_status",
        ),
        sa.CheckConstraint(
            "durability_class IN ('session_affine')",
            name="ck_lab_runtime_sessions_durability",
        ),
        sa.CheckConstraint(
            "fencing_epoch >= 0", name="ck_lab_runtime_sessions_epoch"
        ),
        sa.CheckConstraint(
            "provider_cursor_committed >= 0 AND provider_cursor_acked >= 0 "
            "AND provider_cursor_acked <= provider_cursor_committed",
            name="ck_lab_runtime_sessions_cursors",
        ),
    )
    op.create_index(
        "ix_lab_runtime_sessions_run_id", "lab_runtime_sessions", ["run_id"]
    )
    op.create_index(
        "ix_lab_runtime_sessions_status", "lab_runtime_sessions", ["status"]
    )

    op.create_table(
        "lab_runtime_turns",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("lab_runtime_sessions.id"),
            nullable=False,
        ),
        sa.Column("turn_id", sa.String(length=100), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "provider_cursor", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("final_digest", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "id", "session_id", name="uq_lab_runtime_turns_id_session"
        ),
        sa.UniqueConstraint(
            "session_id", "turn_id", name="uq_lab_runtime_turns_session_turn"
        ),
        sa.UniqueConstraint(
            "session_id", "sequence", name="uq_lab_runtime_turns_session_sequence"
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_lab_runtime_turns_sequence"),
        sa.CheckConstraint(
            "status IN ('ready','intent_pending','result_recorded','runtime_acked',"
            "'completed','final','failed')",
            name="ck_lab_runtime_turns_status",
        ),
        sa.CheckConstraint(
            "provider_cursor >= 0", name="ck_lab_runtime_turns_provider_cursor"
        ),
    )
    op.create_index(
        "ix_lab_runtime_turns_session_id", "lab_runtime_turns", ["session_id"]
    )
    op.create_index(
        "ix_lab_runtime_turns_status", "lab_runtime_turns", ["status"]
    )

    op.create_table(
        "lab_runtime_intents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column(
            "runtime_turn_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column("intent_id", sa.String(length=100), nullable=False),
        sa.Column("action_id", sa.String(length=100), nullable=True),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("args_digest", sa.String(length=64), nullable=False),
        sa.Column("args_redacted_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider_cursor", sa.Integer(), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["runtime_turn_id", "session_id"],
            ["lab_runtime_turns.id", "lab_runtime_turns.session_id"],
            name="fk_lab_runtime_intents_turn_session",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "fencing_epoch"],
            ["lab_runtime_sessions.id", "lab_runtime_sessions.fencing_epoch"],
            name="fk_lab_runtime_intents_session_epoch",
        ),
        sa.UniqueConstraint(
            "session_id", "intent_id", name="uq_lab_runtime_intents_session_intent"
        ),
        sa.UniqueConstraint("action_id", name="uq_lab_runtime_intents_action"),
        sa.UniqueConstraint(
            "id",
            "session_id",
            "runtime_turn_id",
            "intent_id",
            "action_id",
            "fencing_epoch",
            name="uq_lab_runtime_intents_result_binding",
        ),
        sa.CheckConstraint(
            "status IN ('pending','result_recorded','runtime_acked','cancelled','failed')",
            name="ck_lab_runtime_intents_status",
        ),
        sa.CheckConstraint(
            "provider_cursor >= 0", name="ck_lab_runtime_intents_provider_cursor"
        ),
        sa.CheckConstraint(
            "fencing_epoch >= 0", name="ck_lab_runtime_intents_epoch"
        ),
    )
    op.create_index(
        "ix_lab_runtime_intents_session_id", "lab_runtime_intents", ["session_id"]
    )
    op.create_index(
        "ix_lab_runtime_intents_runtime_turn_id",
        "lab_runtime_intents",
        ["runtime_turn_id"],
    )
    op.create_index(
        "ix_lab_runtime_intents_status", "lab_runtime_intents", ["status"]
    )

    op.create_table(
        "lab_runtime_results",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column(
            "runtime_turn_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column(
            "runtime_intent_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column("intent_id", sa.String(length=100), nullable=False),
        sa.Column("action_id", sa.String(length=100), nullable=False),
        sa.Column("command_id", sa.String(length=100), nullable=False),
        sa.Column("receipt_id", sa.String(length=100), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("result_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False),
        sa.Column("runtime_acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            [
                "runtime_intent_id",
                "session_id",
                "runtime_turn_id",
                "intent_id",
                "action_id",
                "fencing_epoch",
            ],
            [
                "lab_runtime_intents.id",
                "lab_runtime_intents.session_id",
                "lab_runtime_intents.runtime_turn_id",
                "lab_runtime_intents.intent_id",
                "lab_runtime_intents.action_id",
                "lab_runtime_intents.fencing_epoch",
            ],
            name="fk_lab_runtime_results_intent_binding",
        ),
        sa.UniqueConstraint("command_id", name="uq_lab_runtime_results_command"),
        sa.UniqueConstraint("receipt_id", name="uq_lab_runtime_results_receipt"),
        sa.UniqueConstraint(
            "runtime_intent_id", name="uq_lab_runtime_results_intent_row"
        ),
        sa.UniqueConstraint(
            "session_id", "intent_id", name="uq_lab_runtime_results_session_intent"
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded','denied','failed')",
            name="ck_lab_runtime_results_outcome",
        ),
        sa.CheckConstraint(
            "fencing_epoch >= 0", name="ck_lab_runtime_results_epoch"
        ),
    )
    op.create_index(
        "ix_lab_runtime_results_session_id", "lab_runtime_results", ["session_id"]
    )
    op.create_index(
        "ix_lab_runtime_results_runtime_turn_id",
        "lab_runtime_results",
        ["runtime_turn_id"],
    )

    op.create_table(
        "lab_run_control_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("lab_runs.id"), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("active_key", sa.String(length=200), nullable=True),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_owner", sa.String(length=100), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fenced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executor_stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_lab_run_control_requests_idempotency"
        ),
        sa.UniqueConstraint("active_key", name="uq_lab_run_control_requests_active"),
        sa.CheckConstraint(
            "action IN ('cancel','terminate','kill')",
            name="ck_lab_run_control_requests_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed','cancelled',"
            "'quarantined')",
            name="ck_lab_run_control_requests_status",
        ),
        sa.CheckConstraint(
            "fencing_epoch >= 0", name="ck_lab_run_control_requests_epoch"
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_lab_run_control_requests_attempts"
        ),
    )
    op.create_index(
        "ix_lab_run_control_requests_run_id", "lab_run_control_requests", ["run_id"]
    )
    op.create_index(
        "ix_lab_run_control_requests_status", "lab_run_control_requests", ["status"]
    )

    if dialect == "postgresql":
        op.execute(PROTOCOL_TRIGGER_FUNCTION)
        op.execute(
            "DROP TRIGGER IF EXISTS trg_guard_lab_run_protocol_version ON public.lab_runs"
        )
        op.execute(
            "CREATE TRIGGER trg_guard_lab_run_protocol_version "
            "BEFORE UPDATE OF protocol_version ON public.lab_runs "
            "FOR EACH ROW EXECUTE FUNCTION public.guard_lab_run_protocol_version()"
        )


def _drop_protocol_column(dialect: str) -> None:
    if dialect == "postgresql":
        op.drop_constraint(
            "ck_lab_runs_protocol_version", "lab_runs", type_="check"
        )
        op.drop_column("lab_runs", "protocol_version")
        return
    with op.batch_alter_table("lab_runs") as batch:
        batch.drop_constraint("ck_lab_runs_protocol_version", type_="check")
        batch.drop_column("protocol_version")


def _enqueue_downgrade_counts(bind) -> tuple[int, int]:
    outbox = sa.table(
        "outbox_events",
        sa.column("topic", sa.String()),
        sa.column("payload_json", sa.JSON()),
    )
    v2_rows = 0
    invalid_rows = 0
    rows = bind.execute(
        sa.select(outbox.c.payload_json).where(
            outbox.c.topic == "lab.run.enqueue"
        )
    ).scalars()
    for payload in rows:
        if not isinstance(payload, dict):
            invalid_rows += 1
            continue
        version = payload.get("protocol_version")
        if type(version) is not int or version not in (1, 2):
            invalid_rows += 1
        elif version == 2:
            v2_rows += 1
    return v2_rows, invalid_rows


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        bind.execute(
            sa.text(
                "LOCK TABLE lab_runs, lab_runtime_sessions, lab_runtime_turns, "
                "lab_runtime_intents, lab_runtime_results, lab_run_control_requests, "
                "outbox_events "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    history = bind.execute(
        sa.text(
            "SELECT "
            "(SELECT count(*) FROM lab_runs WHERE protocol_version = 2) AS v2_runs, "
            "(SELECT count(*) FROM lab_runtime_sessions) AS runtime_sessions, "
            "(SELECT count(*) FROM lab_runtime_turns) AS runtime_turns, "
            "(SELECT count(*) FROM lab_runtime_intents) AS runtime_intents, "
            "(SELECT count(*) FROM lab_runtime_results) AS runtime_results, "
            "(SELECT count(*) FROM lab_run_control_requests) AS control_requests"
        )
    ).mappings().one()
    counts = {key: int(value) for key, value in history.items()}
    v2_outbox, invalid_outbox = _enqueue_downgrade_counts(bind)
    counts["v2_enqueue_outbox"] = v2_outbox
    counts["invalid_enqueue_outbox"] = invalid_outbox
    if any(counts.values()):
        details = ", ".join(f"{key}={value}" for key, value in counts.items())
        raise RuntimeError(
            f"refusing Lab protocol-v2 downgrade: durable state exists ({details})"
        )

    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_guard_lab_run_protocol_version ON public.lab_runs"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS public.guard_lab_run_protocol_version()"
        )

    op.drop_index(
        "ix_lab_run_control_requests_status", table_name="lab_run_control_requests"
    )
    op.drop_index(
        "ix_lab_run_control_requests_run_id", table_name="lab_run_control_requests"
    )
    op.drop_table("lab_run_control_requests")
    op.drop_index(
        "ix_lab_runtime_results_runtime_turn_id", table_name="lab_runtime_results"
    )
    op.drop_index(
        "ix_lab_runtime_results_session_id", table_name="lab_runtime_results"
    )
    op.drop_table("lab_runtime_results")
    op.drop_index("ix_lab_runtime_intents_status", table_name="lab_runtime_intents")
    op.drop_index(
        "ix_lab_runtime_intents_runtime_turn_id", table_name="lab_runtime_intents"
    )
    op.drop_index(
        "ix_lab_runtime_intents_session_id", table_name="lab_runtime_intents"
    )
    op.drop_table("lab_runtime_intents")
    op.drop_index("ix_lab_runtime_turns_status", table_name="lab_runtime_turns")
    op.drop_index(
        "ix_lab_runtime_turns_session_id", table_name="lab_runtime_turns"
    )
    op.drop_table("lab_runtime_turns")
    op.drop_index("ix_lab_runtime_sessions_status", table_name="lab_runtime_sessions")
    op.drop_index(
        "ix_lab_runtime_sessions_run_id", table_name="lab_runtime_sessions"
    )
    op.drop_table("lab_runtime_sessions")
    _drop_protocol_column(dialect)
