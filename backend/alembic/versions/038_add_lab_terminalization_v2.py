"""Enforce the Lab v2 financial terminalization boundary.

Revision ID: 038_add_lab_terminalization_v2
Revises: 037_add_lab_worker_attempts
Create Date: 2026-07-21

The role and function boundary is PostgreSQL-only.  Portable table/constraint
DDL remains useful to local metadata tooling, but release evidence must run on
real PostgreSQL and probe the roles with separate login connections.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "038_add_lab_terminalization_v2"
down_revision = "037_add_lab_worker_attempts"
branch_labels = None
depends_on = None


KERNEL_OWNER = "lab_financial_kernel_owner"
SUBMITTER = "lab_command_submitter_v2"
TERMINALIZER = "lab_terminalizer_v2"
BREAKGLASS = "lab_terminalizer_breakglass"


CHECKPOINT_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION public.lab_terminalization_checkpoint(p_point text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    IF current_setting('simverse.release_disposable', true) = 'on'
       AND current_setting('simverse.lab_terminalization_fault', true) = p_point THEN
        RAISE EXCEPTION 'injected Lab terminalization fault at %', p_point
            USING ERRCODE = 'P0001';
    END IF;
END
$function$;
"""


STABLE_ID_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION public.lab_terminalization_stable_id(
    p_kind text,
    p_key text
) RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_hash text;
BEGIN
    v_hash := encode(sha256(convert_to(p_kind || ':' || p_key, 'UTF8')), 'hex');
    RETURN substring(v_hash FROM 1 FOR 8) || '-' ||
           substring(v_hash FROM 9 FOR 4) || '-5' ||
           substring(v_hash FROM 14 FOR 3) || '-a' ||
           substring(v_hash FROM 18 FOR 3) || '-' ||
           substring(v_hash FROM 21 FOR 12);
END
$function$;
"""


CANONICAL_PAYLOAD_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION public.canonical_lab_terminalization_payload(
    p_operation text,
    p_task_id text,
    p_hold_id text,
    p_expected_epoch bigint
) RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_task public.lab_tasks%ROWTYPE;
    v_hold public.coin_holds%ROWTYPE;
    v_run record;
    v_expected_statuses jsonb;
    v_target_status text;
    v_terminal_action text;
    v_reason text;
    v_idempotency_key text;
    v_command_id text;
    v_creator_id text;
    v_creator_amount bigint;
    v_treasury_amount bigint;
    v_splits jsonb := '[]'::jsonb;
    v_total bigint;
    v_model_cost_sc bigint := 0;
    v_refund_sc bigint;
    v_cost_rate bigint;
BEGIN
    SELECT * INTO v_task FROM public.lab_tasks WHERE id = p_task_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'terminalization task not found' USING ERRCODE = 'P0002';
    END IF;
    SELECT * INTO v_hold FROM public.coin_holds WHERE id = p_hold_id;
    IF NOT FOUND OR v_task.hold_id IS DISTINCT FROM v_hold.id THEN
        RAISE EXCEPTION 'terminalization hold binding is invalid'
            USING ERRCODE = '23514';
    END IF;

    CASE p_operation
        WHEN 'accept' THEN
            v_expected_statuses := '["review"]'::jsonb;
            v_target_status := 'completed';
            v_terminal_action := 'settle';
        WHEN 'auto_release' THEN
            v_expected_statuses := '["review"]'::jsonb;
            v_target_status := 'completed';
            v_terminal_action := 'settle';
        WHEN 'arbitrate_settle' THEN
            v_expected_statuses := '["rejected"]'::jsonb;
            v_target_status := 'completed';
            v_terminal_action := 'settle';
        WHEN 'arbitrate_refund' THEN
            v_expected_statuses := '["rejected"]'::jsonb;
            v_target_status := 'cancelled';
            v_terminal_action := 'refund';
        WHEN 'fail' THEN
            v_expected_statuses := '["assigned","running"]'::jsonb;
            v_target_status := 'failed';
            v_terminal_action := 'refund';
        WHEN 'cancel' THEN
            v_expected_statuses := '["funded","assigned","running"]'::jsonb;
            v_target_status := 'cancelled';
            v_terminal_action := 'refund';
        WHEN 'expire' THEN
            v_expected_statuses := '["funded","assigned","running"]'::jsonb;
            v_target_status := 'expired';
            v_terminal_action := 'refund';
        ELSE
            RAISE EXCEPTION 'unsupported terminalization operation'
                USING ERRCODE = '22023';
    END CASE;

    v_reason := 'lab_' || p_operation || ':' || v_task.id;
    v_idempotency_key := p_operation || ':' || v_task.id || ':' ||
                         v_hold.id || ':' || p_expected_epoch::text;
    v_command_id := public.lab_terminalization_stable_id(
        'command', v_idempotency_key
    );

    IF v_terminal_action = 'refund' THEN
        IF p_operation IN ('fail', 'cancel', 'expire')
           AND v_task.accepted_run_id IS NOT NULL THEN
            SELECT * INTO v_run
              FROM public.lab_runs
             WHERE id = v_task.accepted_run_id AND task_id = v_task.id;
            IF FOUND AND v_run.adapter <> 'mock' THEN
                IF COALESCE(v_run.error, '') LIKE 'cost_unknown:%' THEN
                    RAISE EXCEPTION 'model cost is unknown; refund settlement is blocked'
                        USING ERRCODE = '55000';
                END IF;
                -- The rate is frozen by migration 053. The fallback keeps a
                -- standalone 038 cohort on the declared 100 SC/USD baseline.
                v_cost_rate := COALESCE(
                    NULLIF(to_jsonb(v_run)->>'model_cost_sc_per_usd', '')::bigint,
                    100
                );
                IF v_cost_rate <= 0 THEN
                    RAISE EXCEPTION 'model cost conversion rate is invalid'
                        USING ERRCODE = '23514';
                END IF;
                v_model_cost_sc := LEAST(
                    v_hold.amount,
                    CEIL(
                        GREATEST(COALESCE(v_run.cost_usd_cents, 0), 0)::numeric
                        * v_cost_rate::numeric / 100
                    )::bigint
                );
            END IF;
        END IF;
        v_refund_sc := v_hold.amount - v_model_cost_sc;
        IF v_refund_sc > 0 THEN
            v_splits := v_splits || jsonb_build_array(jsonb_build_object(
                'recipient_key', v_hold.user_id,
                'amount', v_refund_sc,
                'reason', v_reason
            ));
        END IF;
        IF v_model_cost_sc > 0 THEN
            v_splits := v_splits || jsonb_build_array(jsonb_build_object(
                'recipient_key', 'sink',
                'amount', v_model_cost_sc,
                'reason', 'lab_model_cost:' || v_task.id
            ));
        END IF;
    ELSE
        IF v_task.terminal_creator_share_bps IS NULL
           OR v_task.terminal_creator_share_bps NOT BETWEEN 0 AND 10000 THEN
            RAISE EXCEPTION 'settlement creator-share policy is not frozen'
                USING ERRCODE = '23514';
        END IF;
        IF v_task.researcher_slug IS NULL THEN
            RAISE EXCEPTION 'settlement researcher binding is missing'
                USING ERRCODE = '23514';
        END IF;
        SELECT creator_id INTO v_creator_id
          FROM public.residents
         WHERE slug = v_task.researcher_slug;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'settlement researcher does not exist'
                USING ERRCODE = '23503';
        END IF;

        v_creator_amount :=
            (v_task.reward_sc::bigint * v_task.terminal_creator_share_bps) / 10000;
        v_treasury_amount := v_task.reward_sc - v_creator_amount;
        IF v_creator_id IS NOT NULL
           AND v_creator_id <> 'system'
           AND v_creator_amount > 0 THEN
            v_splits := v_splits || jsonb_build_array(jsonb_build_object(
                'recipient_key', v_creator_id,
                'amount', v_creator_amount,
                'reason', 'lab_reward:' || v_task.id
            ));
        ELSE
            v_treasury_amount := v_task.reward_sc;
        END IF;
        IF v_treasury_amount > 0 THEN
            v_splits := v_splits || jsonb_build_array(jsonb_build_object(
                'recipient_key', 'treasury:' || v_task.researcher_slug,
                'amount', v_treasury_amount,
                'reason', 'lab_treasury:' || v_task.id
            ));
        END IF;
        IF v_task.platform_fee_sc > 0 THEN
            v_splits := v_splits || jsonb_build_array(jsonb_build_object(
                'recipient_key', 'sink',
                'amount', v_task.platform_fee_sc,
                'reason', 'lab_fee:' || v_task.id
            ));
        END IF;
    END IF;

    SELECT COALESCE(sum((split.value->>'amount')::bigint), 0)
      INTO v_total
      FROM jsonb_array_elements(v_splits) AS split(value);
    IF jsonb_array_length(v_splits) = 0 OR v_total <> v_hold.amount THEN
        RAISE EXCEPTION 'canonical terminalization distribution is not conservative'
            USING ERRCODE = '23514';
    END IF;

    RETURN jsonb_build_object(
        'schema', 'simverse.lab.terminalization-command.v2',
        'expected_task_statuses', v_expected_statuses,
        'target_status', v_target_status,
        'terminal_action', v_terminal_action,
        'reason', v_reason,
        'completed_at', v_target_status = 'completed',
        'event_id', public.lab_terminalization_stable_id('event', v_command_id),
        'receipt_id', public.lab_terminalization_stable_id('receipt', v_command_id),
        'splits', v_splits
    );
END
$function$;
"""


SUBMIT_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION public.submit_lab_terminalization_command(
    p_operation text,
    p_task_id text,
    p_actor text,
    p_expected_epoch bigint
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_task public.lab_tasks%ROWTYPE;
    v_hold public.coin_holds%ROWTYPE;
    v_live_epoch bigint;
    v_expected_statuses jsonb;
    v_idempotency_key text;
    v_command_id text;
    v_payload jsonb;
    v_expected_payload jsonb;
    v_expected_idempotency_key text;
    v_expected_command_id text;
    v_existing public.lab_terminalization_commands%ROWTYPE;
BEGIN
    IF COALESCE(p_operation, '') = '' OR COALESCE(p_task_id, '') = ''
       OR COALESCE(p_actor, '') = '' OR p_expected_epoch < 0 THEN
        RAISE EXCEPTION 'invalid terminalization submission'
            USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_task
      FROM public.lab_tasks
     WHERE id = p_task_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'terminalization task not found' USING ERRCODE = 'P0002';
    END IF;
    SELECT * INTO v_hold
      FROM public.coin_holds
     WHERE id = v_task.hold_id
     FOR UPDATE;
    IF NOT FOUND OR v_hold.user_id IS DISTINCT FROM v_task.issuer_user_id
       OR v_hold.reason IS DISTINCT FROM ('lab_task:' || v_task.id)
       OR v_hold.status <> 'held'
       OR v_hold.terminalization_version <> 'v2'
       OR v_hold.cutover_at IS NULL THEN
        RAISE EXCEPTION 'task escrow is not an eligible held v2 cohort'
            USING ERRCODE = '55000';
    END IF;

    CASE p_operation
        WHEN 'accept' THEN
            v_expected_statuses := '["review"]'::jsonb;
            IF p_actor <> v_task.issuer_user_id THEN
                RAISE EXCEPTION 'accept actor is not the task issuer'
                    USING ERRCODE = '42501';
            END IF;
        WHEN 'auto_release' THEN
            v_expected_statuses := '["review"]'::jsonb;
            IF p_actor <> 'scheduler:auto-release'
               OR v_task.review_deadline_at IS NULL
               OR v_task.review_deadline_at > pg_catalog.clock_timestamp() THEN
                RAISE EXCEPTION 'auto-release authority or deadline is invalid'
                    USING ERRCODE = '42501';
            END IF;
        WHEN 'arbitrate_settle', 'arbitrate_refund' THEN
            v_expected_statuses := '["rejected"]'::jsonb;
            IF NOT EXISTS (
                SELECT 1 FROM public.users
                 WHERE id = p_actor AND is_admin IS TRUE
            ) THEN
                RAISE EXCEPTION 'arbitration actor is not an admin'
                    USING ERRCODE = '42501';
            END IF;
        WHEN 'fail' THEN
            v_expected_statuses := '["assigned","running"]'::jsonb;
            IF v_task.accepted_run_id IS NULL
               OR p_actor <> ('runner:' || v_task.accepted_run_id) THEN
                RAISE EXCEPTION 'fail actor binding mismatch'
                    USING ERRCODE = '42501';
            END IF;
        WHEN 'cancel' THEN
            v_expected_statuses := '["funded","assigned","running"]'::jsonb;
            IF p_actor <> v_task.issuer_user_id THEN
                RAISE EXCEPTION 'cancel actor is not the task issuer'
                    USING ERRCODE = '42501';
            END IF;
        WHEN 'expire' THEN
            v_expected_statuses := '["funded","assigned","running"]'::jsonb;
            IF p_actor <> 'scheduler:expire'
               OR v_task.deadline_at > pg_catalog.clock_timestamp() THEN
                RAISE EXCEPTION 'expiry authority or deadline is invalid'
                    USING ERRCODE = '42501';
            END IF;
        ELSE
            RAISE EXCEPTION 'unsupported terminalization operation'
                USING ERRCODE = '22023';
    END CASE;
    IF v_task.status <> ALL (
        SELECT value FROM jsonb_array_elements_text(v_expected_statuses)
    ) THEN
        RAISE EXCEPTION 'task is not eligible for terminalization operation'
            USING ERRCODE = '55000';
    END IF;

    IF v_task.accepted_run_id IS NULL THEN
        v_live_epoch := 0;
    ELSE
        SELECT fencing_epoch INTO v_live_epoch
          FROM public.lab_run_leases
         WHERE run_id = v_task.accepted_run_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'accepted v2 run has no fencing lease'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF v_live_epoch <> p_expected_epoch THEN
        RAISE EXCEPTION 'terminalization submission epoch mismatch'
            USING ERRCODE = '40001';
    END IF;

    v_idempotency_key := p_operation || ':' || v_task.id || ':' ||
                         v_hold.id || ':' || p_expected_epoch::text;
    v_command_id := public.lab_terminalization_stable_id(
        'command', v_idempotency_key
    );
    v_payload := public.canonical_lab_terminalization_payload(
        p_operation, v_task.id, v_hold.id, p_expected_epoch
    );
    INSERT INTO public.lab_terminalization_commands(
        command_id, operation, task_id, hold_id, actor, expected_epoch,
        idempotency_key, status, payload_json, created_at
    ) VALUES (
        v_command_id, p_operation, v_task.id, v_hold.id, p_actor,
        p_expected_epoch, v_idempotency_key, 'pending', v_payload,
        pg_catalog.clock_timestamp()
    ) ON CONFLICT (command_id) DO NOTHING;

    SELECT * INTO v_existing
      FROM public.lab_terminalization_commands
     WHERE command_id = v_command_id;
    IF v_existing.operation IS DISTINCT FROM p_operation
       OR v_existing.task_id IS DISTINCT FROM v_task.id
       OR v_existing.hold_id IS DISTINCT FROM v_hold.id
       OR v_existing.actor IS DISTINCT FROM p_actor
       OR v_existing.expected_epoch IS DISTINCT FROM p_expected_epoch
       OR v_existing.idempotency_key IS DISTINCT FROM v_idempotency_key
       OR v_existing.payload_json::jsonb IS DISTINCT FROM v_payload THEN
        RAISE EXCEPTION 'terminalization command retry binding changed'
            USING ERRCODE = '23514';
    END IF;
    RETURN v_command_id;
END
$function$;
"""


FINALIZE_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION public.finalize_lab_terminalization(
    p_command_id text,
    p_expected_epoch bigint
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_seed_task_id text;
    v_seed_hold_id text;
    v_command public.lab_terminalization_commands%ROWTYPE;
    v_task public.lab_tasks%ROWTYPE;
    v_hold public.coin_holds%ROWTYPE;
    v_payload jsonb;
    v_expected_payload jsonb;
    v_expected_idempotency_key text;
    v_expected_command_id text;
    v_split jsonb;
    v_recipient text;
    v_reason text;
    v_amount bigint;
    v_total bigint := 0;
    v_count integer := 0;
    v_user_count integer := 0;
    v_required_user_count integer := 0;
    v_terminal_action text;
    v_expected_statuses jsonb;
    v_target_status text;
    v_receipt_id text;
    v_event_id text;
    v_result_digest text;
    v_run_status text;
    v_run_count integer;
    v_lease_epoch bigint;
    v_completed_at boolean;
    v_effect jsonb;
    v_run record;
    v_expected_model_cost_sc bigint := 0;
    v_expected_refund_sc bigint;
    v_expected_refund_count integer;
    v_cost_rate bigint;
BEGIN
    IF p_command_id IS NULL OR p_command_id = '' OR p_expected_epoch < 0 THEN
        RAISE EXCEPTION 'invalid terminalization identity or epoch'
            USING ERRCODE = '22023';
    END IF;

    SELECT task_id, hold_id INTO v_seed_task_id, v_seed_hold_id
      FROM public.lab_terminalization_commands
     WHERE command_id = p_command_id
    ;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'terminalization command not found'
            USING ERRCODE = 'P0002';
    END IF;

    SELECT * INTO v_task
      FROM public.lab_tasks
     WHERE id = v_seed_task_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'terminalization task not found' USING ERRCODE = 'P0002';
    END IF;

    SELECT * INTO v_hold
      FROM public.coin_holds
     WHERE id = v_seed_hold_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'terminalization hold not found' USING ERRCODE = 'P0002';
    END IF;

    SELECT * INTO v_command
      FROM public.lab_terminalization_commands
     WHERE command_id = p_command_id
     FOR UPDATE;
    IF v_command.task_id IS DISTINCT FROM v_task.id
       OR v_command.hold_id IS DISTINCT FROM v_hold.id
       OR v_task.hold_id IS DISTINCT FROM v_hold.id THEN
        RAISE EXCEPTION 'command task/hold binding mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF v_command.expected_epoch <> p_expected_epoch THEN
        RAISE EXCEPTION 'terminalization command epoch mismatch'
            USING ERRCODE = '40001';
    END IF;
    v_payload := v_command.payload_json::jsonb;
    v_expected_idempotency_key := v_command.operation || ':' || v_task.id || ':' ||
                                  v_hold.id || ':' || p_expected_epoch::text;
    v_expected_command_id := public.lab_terminalization_stable_id(
        'command', v_expected_idempotency_key
    );
    v_expected_payload := public.canonical_lab_terminalization_payload(
        v_command.operation, v_task.id, v_hold.id, p_expected_epoch
    );
    IF v_command.command_id IS DISTINCT FROM v_expected_command_id
       OR v_command.idempotency_key IS DISTINCT FROM v_expected_idempotency_key
       OR v_payload IS DISTINCT FROM v_expected_payload THEN
        RAISE EXCEPTION 'terminalization command is not canonical'
            USING ERRCODE = '23514';
    END IF;
    IF v_payload->>'schema' <> 'simverse.lab.terminalization-command.v2' THEN
        RAISE EXCEPTION 'unsupported terminalization command schema'
            USING ERRCODE = '22023';
    END IF;
    IF v_hold.user_id IS DISTINCT FROM v_task.issuer_user_id
       OR v_hold.reason IS DISTINCT FROM ('lab_task:' || v_task.id) THEN
        RAISE EXCEPTION 'hold ownership binding mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF v_command.status = 'completed' THEN
        SELECT receipt_id INTO v_receipt_id
          FROM public.lab_terminalization_receipts
         WHERE command_id = p_command_id;
        IF v_receipt_id IS NULL THEN
            RAISE EXCEPTION 'completed command has no receipt'
                USING ERRCODE = '23514';
        END IF;
        RETURN v_receipt_id;
    END IF;
    IF v_command.status <> 'pending' THEN
        RAISE EXCEPTION 'terminalization command is not pending'
            USING ERRCODE = '55000';
    END IF;
    IF v_hold.status <> 'held' OR v_hold.terminalization_version <> 'v2'
       OR v_hold.cutover_at IS NULL THEN
        RAISE EXCEPTION 'hold is not an eligible held v2 cohort'
            USING ERRCODE = '55000';
    END IF;

    CASE v_command.operation
        WHEN 'accept' THEN
            v_expected_statuses := '["review"]'::jsonb;
            v_target_status := 'completed';
            v_terminal_action := 'settle';
            IF v_command.actor <> v_task.issuer_user_id THEN
                RAISE EXCEPTION 'accept actor is not the task issuer' USING ERRCODE = '42501';
            END IF;
        WHEN 'auto_release' THEN
            v_expected_statuses := '["review"]'::jsonb;
            v_target_status := 'completed';
            v_terminal_action := 'settle';
            IF v_command.actor <> 'scheduler:auto-release' THEN
                RAISE EXCEPTION 'auto-release actor binding mismatch' USING ERRCODE = '42501';
            END IF;
            IF v_task.review_deadline_at IS NULL
               OR v_task.review_deadline_at > pg_catalog.clock_timestamp() THEN
                RAISE EXCEPTION 'task has not reached its auto-release deadline'
                    USING ERRCODE = '55000';
            END IF;
        WHEN 'arbitrate_settle' THEN
            v_expected_statuses := '["rejected"]'::jsonb;
            v_target_status := 'completed';
            v_terminal_action := 'settle';
            IF NOT EXISTS (
                SELECT 1 FROM public.users
                 WHERE id = v_command.actor AND is_admin IS TRUE
            ) THEN
                RAISE EXCEPTION 'arbitration actor is not an admin' USING ERRCODE = '42501';
            END IF;
        WHEN 'arbitrate_refund' THEN
            v_expected_statuses := '["rejected"]'::jsonb;
            v_target_status := 'cancelled';
            v_terminal_action := 'refund';
            IF NOT EXISTS (
                SELECT 1 FROM public.users
                 WHERE id = v_command.actor AND is_admin IS TRUE
            ) THEN
                RAISE EXCEPTION 'arbitration actor is not an admin' USING ERRCODE = '42501';
            END IF;
        WHEN 'fail' THEN
            v_expected_statuses := '["assigned","running"]'::jsonb;
            v_target_status := 'failed';
            v_terminal_action := 'refund';
            IF v_task.accepted_run_id IS NULL
               OR v_command.actor <> ('runner:' || v_task.accepted_run_id) THEN
                RAISE EXCEPTION 'fail actor binding mismatch' USING ERRCODE = '42501';
            END IF;
        WHEN 'cancel' THEN
            v_expected_statuses := '["funded","assigned","running"]'::jsonb;
            v_target_status := 'cancelled';
            v_terminal_action := 'refund';
            IF v_command.actor <> v_task.issuer_user_id THEN
                RAISE EXCEPTION 'cancel actor is not the task issuer' USING ERRCODE = '42501';
            END IF;
        WHEN 'expire' THEN
            v_expected_statuses := '["funded","assigned","running"]'::jsonb;
            v_target_status := 'expired';
            v_terminal_action := 'refund';
            IF v_command.actor <> 'scheduler:expire' THEN
                RAISE EXCEPTION 'expire actor binding mismatch' USING ERRCODE = '42501';
            END IF;
            IF v_task.deadline_at > pg_catalog.clock_timestamp() THEN
                RAISE EXCEPTION 'task has not reached its expiry deadline'
                    USING ERRCODE = '55000';
            END IF;
        ELSE
            RAISE EXCEPTION 'unsupported terminalization operation'
                USING ERRCODE = '22023';
    END CASE;

    IF v_payload->'expected_task_statuses' IS DISTINCT FROM v_expected_statuses
       OR v_task.status <> ALL (
           SELECT value FROM jsonb_array_elements_text(v_expected_statuses)
       ) THEN
        RAISE EXCEPTION 'task state or expected-state binding mismatch'
            USING ERRCODE = '40001';
    END IF;
    IF v_payload->>'target_status' IS DISTINCT FROM v_target_status THEN
        RAISE EXCEPTION 'target status binding mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF v_payload->>'terminal_action' IS DISTINCT FROM v_terminal_action THEN
        RAISE EXCEPTION 'terminal action binding mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF COALESCE(v_payload->>'reason', '') = ''
       OR length(v_payload->>'reason') > 100 THEN
        RAISE EXCEPTION 'terminalization reason is invalid'
            USING ERRCODE = '22023';
    END IF;
    v_completed_at := COALESCE((v_payload->>'completed_at')::boolean, false);
    IF v_completed_at IS DISTINCT FROM (v_target_status = 'completed') THEN
        RAISE EXCEPTION 'completed_at binding mismatch'
            USING ERRCODE = '23514';
    END IF;
    v_event_id := v_payload->>'event_id';
    v_receipt_id := v_payload->>'receipt_id';
    IF v_event_id IS NULL
       OR v_event_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR v_receipt_id IS NULL
       OR v_receipt_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION 'invalid deterministic receipt identity'
            USING ERRCODE = '22023';
    END IF;

    IF jsonb_typeof(v_payload->'splits') <> 'array'
       OR jsonb_array_length(v_payload->'splits') = 0 THEN
        RAISE EXCEPTION 'terminalization splits must be a non-empty array'
            USING ERRCODE = '22023';
    END IF;

    FOR v_split IN SELECT value FROM jsonb_array_elements(v_payload->'splits')
    LOOP
        IF jsonb_typeof(v_split) <> 'object'
           OR COALESCE(v_split->>'recipient_key', '') = ''
           OR v_split->>'recipient_key' <> btrim(v_split->>'recipient_key')
           OR length(v_split->>'recipient_key') > 160
           OR COALESCE(v_split->>'amount', '') !~ '^[1-9][0-9]*$'
           OR COALESCE(v_split->>'reason', '') = ''
           OR length(v_split->>'reason') > 100
           OR (
               v_split->>'recipient_key' LIKE 'treasury:%'
               AND (
                   length(substring(v_split->>'recipient_key' FROM 10)) NOT BETWEEN 1 AND 100
                   OR substring(v_split->>'recipient_key' FROM 10)
                      <> btrim(substring(v_split->>'recipient_key' FROM 10))
               )
           ) THEN
            RAISE EXCEPTION 'invalid terminalization split'
                USING ERRCODE = '22023';
        END IF;
        v_amount := (v_split->>'amount')::bigint;
        IF v_amount > 2147483647 THEN
            RAISE EXCEPTION 'terminalization split amount exceeds integer range'
                USING ERRCODE = '22003';
        END IF;
        v_total := v_total + v_amount;
        v_count := v_count + 1;
    END LOOP;

    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements(v_payload->'splits') AS split(value)
         GROUP BY split.value->>'recipient_key'
        HAVING count(*) > 1
    ) OR v_total <> v_hold.amount THEN
        RAISE EXCEPTION 'split recipients are duplicated or not conservative'
            USING ERRCODE = '23514';
    END IF;
    -- Lock all existing user recipients in stable id order, then verify none are missing.
    PERFORM 1
      FROM public.users
     WHERE id IN (
        SELECT split.value->>'recipient_key'
          FROM jsonb_array_elements(v_payload->'splits') AS split(value)
         WHERE split.value->>'recipient_key' <> 'sink'
           AND split.value->>'recipient_key' NOT LIKE 'treasury:%'
     )
     ORDER BY id
     FOR UPDATE;
    SELECT count(DISTINCT split.value->>'recipient_key') INTO v_required_user_count
      FROM jsonb_array_elements(v_payload->'splits') AS split(value)
     WHERE split.value->>'recipient_key' <> 'sink'
       AND split.value->>'recipient_key' NOT LIKE 'treasury:%';
    SELECT count(*) INTO v_user_count
      FROM public.users
     WHERE id IN (
        SELECT split.value->>'recipient_key'
          FROM jsonb_array_elements(v_payload->'splits') AS split(value)
         WHERE split.value->>'recipient_key' <> 'sink'
           AND split.value->>'recipient_key' NOT LIKE 'treasury:%'
     );
    IF v_user_count <> v_required_user_count THEN
        RAISE EXCEPTION 'terminalization references a missing user'
            USING ERRCODE = '23503';
    END IF;

    -- Materialize missing treasury accounts in the same stable order before
    -- locking them. The insert remains inside this all-or-nothing transaction.
    INSERT INTO public.resident_treasuries(resident_slug, balance_sc, updated_at)
    SELECT DISTINCT
           substring(split.value->>'recipient_key' FROM 10) AS resident_slug,
           0,
           pg_catalog.clock_timestamp()
      FROM jsonb_array_elements(v_payload->'splits') AS split(value)
     WHERE split.value->>'recipient_key' LIKE 'treasury:%'
     ORDER BY resident_slug
    ON CONFLICT (resident_slug) DO NOTHING;

    -- All treasury rows are locked after Users, in stable slug order.
    PERFORM 1
      FROM public.resident_treasuries
     WHERE resident_slug IN (
        SELECT substring(split.value->>'recipient_key' FROM 10)
          FROM jsonb_array_elements(v_payload->'splits') AS split(value)
         WHERE split.value->>'recipient_key' LIKE 'treasury:%'
     )
     ORDER BY resident_slug
     FOR UPDATE;

    -- Runs are inspected only after the financial lock order is complete.
    PERFORM 1
      FROM public.lab_runs
     WHERE task_id = v_task.id
     ORDER BY id
     FOR UPDATE;
    PERFORM 1
      FROM public.lab_run_leases
     WHERE run_id IN (
         SELECT id FROM public.lab_runs WHERE task_id = v_task.id
     )
     ORDER BY run_id
     FOR UPDATE;

    SELECT count(*) INTO v_run_count
      FROM public.lab_runs
     WHERE task_id = v_task.id;
    IF v_run_count > 1 THEN
        RAISE EXCEPTION 'v2 terminalization requires exactly one linked run'
            USING ERRCODE = '23514';
    ELSIF v_task.accepted_run_id IS NULL AND v_run_count > 0 THEN
        RAISE EXCEPTION 'v2 terminalization has an unbound linked run'
            USING ERRCODE = '23514';
    END IF;

    IF v_task.accepted_run_id IS NOT NULL THEN
        SELECT * INTO v_run
          FROM public.lab_runs
         WHERE id = v_task.accepted_run_id AND task_id = v_task.id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'accepted run binding is missing'
                USING ERRCODE = '23514';
        END IF;
        v_run_status := v_run.status;
        SELECT fencing_epoch INTO v_lease_epoch
          FROM public.lab_run_leases
         WHERE run_id = v_task.accepted_run_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'accepted v2 run has no fencing lease'
                USING ERRCODE = '23514';
        END IF;
        IF v_lease_epoch <> p_expected_epoch THEN
            RAISE EXCEPTION 'run lease epoch mismatch'
                USING ERRCODE = '40001';
        END IF;
    ELSIF p_expected_epoch <> 0 THEN
        RAISE EXCEPTION 'nonzero epoch without an accepted run'
            USING ERRCODE = '23514';
    END IF;

    IF v_terminal_action = 'refund' THEN
        IF v_command.operation IN ('fail', 'cancel', 'expire')
           AND v_task.accepted_run_id IS NOT NULL
           AND v_run.adapter <> 'mock' THEN
            IF COALESCE(v_run.error, '') LIKE 'cost_unknown:%' THEN
                RAISE EXCEPTION 'model cost is unknown; refund settlement is blocked'
                    USING ERRCODE = '55000';
            END IF;
            v_cost_rate := COALESCE(
                NULLIF(to_jsonb(v_run)->>'model_cost_sc_per_usd', '')::bigint,
                100
            );
            IF v_cost_rate <= 0 THEN
                RAISE EXCEPTION 'model cost conversion rate is invalid'
                    USING ERRCODE = '23514';
            END IF;
            v_expected_model_cost_sc := LEAST(
                v_hold.amount,
                CEIL(
                    GREATEST(COALESCE(v_run.cost_usd_cents, 0), 0)::numeric
                    * v_cost_rate::numeric / 100
                )::bigint
            );
        END IF;
        v_expected_refund_sc := v_hold.amount - v_expected_model_cost_sc;
        v_expected_refund_count :=
            CASE WHEN v_expected_refund_sc > 0 THEN 1 ELSE 0 END
            + CASE WHEN v_expected_model_cost_sc > 0 THEN 1 ELSE 0 END;
        IF v_count <> v_expected_refund_count
           OR (
               v_expected_refund_sc > 0 AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(v_payload->'splits') split(value)
                    WHERE split.value->>'recipient_key' = v_hold.user_id
                      AND (split.value->>'amount')::bigint = v_expected_refund_sc
                      AND split.value->>'reason' = v_payload->>'reason'
               )
           )
           OR (
               v_expected_model_cost_sc > 0 AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(v_payload->'splits') split(value)
                    WHERE split.value->>'recipient_key' = 'sink'
                      AND (split.value->>'amount')::bigint = v_expected_model_cost_sc
                      AND split.value->>'reason' = 'lab_model_cost:' || v_task.id
               )
           ) THEN
            RAISE EXCEPTION 'refund does not match the kernel-computed net distribution'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    IF v_terminal_action = 'settle' AND (
        v_task.accepted_run_id IS NULL OR v_run_status <> 'succeeded'
    ) THEN
        RAISE EXCEPTION 'v2 settlement requires a succeeded accepted run'
            USING ERRCODE = '55000';
    ELSIF v_command.operation = 'arbitrate_refund' AND (
        v_task.accepted_run_id IS NULL OR v_run_status <> 'succeeded'
    ) THEN
        RAISE EXCEPTION 'v2 arbitration refund requires a succeeded accepted run'
            USING ERRCODE = '55000';
    ELSIF v_command.operation = 'fail' AND (
        v_task.accepted_run_id IS NULL OR v_run_status NOT IN ('failed', 'cancelled')
    ) THEN
        RAISE EXCEPTION 'v2 failure refund requires a terminal accepted run'
            USING ERRCODE = '55000';
    ELSIF v_command.operation IN ('cancel', 'expire')
          AND v_task.status <> 'funded'
          AND v_task.accepted_run_id IS NULL THEN
        RAISE EXCEPTION 'v2 refund cohort is missing its accepted run'
            USING ERRCODE = '23514';
    END IF;

    IF v_command.operation IN (
        'arbitrate_refund', 'fail', 'cancel', 'expire'
    ) THEN
        IF EXISTS (
            SELECT 1
              FROM public.lab_runs run
             LEFT JOIN public.lab_run_leases lease ON lease.run_id = run.id
             WHERE run.task_id = v_task.id
               AND lease.run_id IS NULL
        ) THEN
            RAISE EXCEPTION 'linked v2 run has no fencing lease'
                USING ERRCODE = '23514';
        END IF;
        UPDATE public.lab_run_leases lease
           SET fencing_epoch = lease.fencing_epoch + 1,
               updated_at = pg_catalog.clock_timestamp()
         WHERE lease.run_id IN (
             SELECT id FROM public.lab_runs
              WHERE task_id = v_task.id
         );
        UPDATE public.lab_runs
           SET status = 'cancelled', ended_at = pg_catalog.clock_timestamp()
         WHERE task_id = v_task.id
           AND status IN ('queued', 'running', 'needs_approval');
    END IF;

    UPDATE public.lab_terminalization_commands
       SET status = 'processing', claimed_at = pg_catalog.clock_timestamp()
     WHERE command_id = p_command_id AND status = 'pending';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'terminalization command ownership was lost'
            USING ERRCODE = '40001';
    END IF;

    FOR v_split IN SELECT value FROM jsonb_array_elements(v_payload->'splits')
    LOOP
        v_recipient := v_split->>'recipient_key';
        v_amount := (v_split->>'amount')::bigint;
        v_reason := v_split->>'reason';
        IF v_recipient = 'sink' THEN
            NULL;
        ELSIF v_recipient LIKE 'treasury:%' THEN
            IF substring(v_recipient FROM 10) = '' THEN
                RAISE EXCEPTION 'empty treasury recipient' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.resident_treasuries(resident_slug, balance_sc, updated_at)
            VALUES (substring(v_recipient FROM 10), 0, pg_catalog.clock_timestamp())
            ON CONFLICT (resident_slug) DO NOTHING;
            UPDATE public.resident_treasuries
               SET balance_sc = balance_sc + v_amount,
                   updated_at = pg_catalog.clock_timestamp()
             WHERE resident_slug = substring(v_recipient FROM 10);
            PERFORM public.lab_terminalization_checkpoint(
                'after_credit:' || v_recipient
            );
        ELSE
            UPDATE public.users
               SET soul_coin_balance = soul_coin_balance + v_amount
             WHERE id = v_recipient;
            INSERT INTO public.transactions(id, user_id, amount, reason, created_at)
            VALUES (
                p_command_id || ':tx:' || md5(v_recipient),
                v_recipient,
                v_amount,
                v_reason,
                pg_catalog.clock_timestamp()
            );
            PERFORM public.lab_terminalization_checkpoint(
                'after_credit:' || v_recipient
            );
        END IF;

        INSERT INTO public.coin_hold_entries(
            id, hold_id, terminal_action, recipient_key, amount,
            operation_key, reason, created_at
        ) VALUES (
            p_command_id || ':entry:' || md5(v_recipient),
            v_hold.id,
            v_terminal_action,
            v_recipient,
            v_amount,
            p_command_id || ':' || v_recipient,
            v_reason,
            pg_catalog.clock_timestamp()
        );
        PERFORM public.lab_terminalization_checkpoint(
            'after_distribution:' || v_recipient
        );
    END LOOP;

    UPDATE public.coin_holds
       SET status = CASE v_terminal_action WHEN 'settle' THEN 'settled' ELSE 'refunded' END,
           settled_at = pg_catalog.clock_timestamp()
     WHERE id = v_hold.id AND status = 'held';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'held ownership CAS failed' USING ERRCODE = '40001';
    END IF;
    PERFORM public.lab_terminalization_checkpoint('after_hold');

    UPDATE public.lab_tasks
       SET status = v_target_status,
           completed_at = CASE WHEN v_completed_at THEN pg_catalog.clock_timestamp() ELSE NULL END,
           updated_at = pg_catalog.clock_timestamp()
     WHERE id = v_task.id AND status = v_task.status;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'task terminal CAS failed' USING ERRCODE = '40001';
    END IF;
    PERFORM public.lab_terminalization_checkpoint('after_task');

    v_effect := jsonb_build_object(
        'command_id', p_command_id,
        'operation', v_command.operation,
        'task_id', v_task.id,
        'hold_id', v_hold.id,
        'target_status', v_target_status,
        'terminal_action', v_terminal_action,
        'amount', v_hold.amount,
        'journal_count', v_count,
        'event_id', v_event_id
    );
    v_result_digest := encode(
        sha256(convert_to(v_effect::text, 'UTF8')),
        'hex'
    );

    INSERT INTO public.outbox_events(
        event_id, tenant_id, run_id, topic, payload_json, created_at
    ) VALUES (
        v_event_id,
        v_task.issuer_user_id,
        v_task.accepted_run_id,
        'lab.task.terminalized',
        jsonb_build_object(
            'type', 'lab.task.terminalized',
            'schema_version', 2,
            'event_id', v_event_id,
            'receipt_id', v_receipt_id
        ) || v_effect,
        pg_catalog.clock_timestamp()
    );
    PERFORM public.lab_terminalization_checkpoint('after_outbox');

    INSERT INTO public.lab_terminalization_receipts(
        receipt_id, command_id, task_id, hold_id, operation, event_id,
        amount, journal_count, result_digest, payload_json, created_at
    ) VALUES (
        v_receipt_id,
        p_command_id,
        v_task.id,
        v_hold.id,
        v_command.operation,
        v_event_id,
        v_hold.amount,
        v_count,
        v_result_digest,
        v_effect,
        pg_catalog.clock_timestamp()
    );
    PERFORM public.lab_terminalization_checkpoint('after_receipt');

    UPDATE public.lab_terminalization_commands
       SET status = 'completed', completed_at = pg_catalog.clock_timestamp(), last_error = NULL
     WHERE command_id = p_command_id AND status = 'processing';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'terminalization completion CAS failed' USING ERRCODE = '40001';
    END IF;
    PERFORM public.lab_terminalization_checkpoint('before_commit');
    RETURN v_receipt_id;
END
$function$;
"""


GUARD_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION public.guard_lab_terminal_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_terminalization_version text;
BEGIN
    IF TG_TABLE_NAME = 'coin_holds' THEN
        IF OLD.terminalization_version = 'v1'
           AND NEW.terminalization_version = 'v1' THEN
            RETURN NEW;
        END IF;
    ELSIF TG_TABLE_NAME = 'lab_tasks' THEN
        IF OLD.terminal_creator_share_bps IS NOT NULL
           AND OLD.terminal_creator_share_bps IS DISTINCT FROM
               NEW.terminal_creator_share_bps
           AND current_user <> 'lab_financial_kernel_owner' THEN
            RAISE EXCEPTION 'Lab terminal creator-share policy is immutable'
                USING ERRCODE = '42501';
        END IF;
        SELECT terminalization_version INTO v_terminalization_version
          FROM public.coin_holds
         WHERE id = COALESCE(NEW.hold_id, OLD.hold_id);
        IF v_terminalization_version IS DISTINCT FROM 'v2' THEN
            RETURN NEW;
        END IF;
    END IF;
    IF current_user <> 'lab_financial_kernel_owner' THEN
        RAISE EXCEPTION 'Lab terminal state is writable only through finalize_lab_terminalization'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$function$;
"""


APPEND_ONLY_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION public.reject_lab_compensation_history_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path TO pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION 'Lab compensation history is append-only'
        USING ERRCODE = '55000';
END
$function$;
"""


BREAKGLASS_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION public.apply_lab_breakglass_compensation(
    p_operation_key text,
    p_ticket text,
    p_reason text,
    p_actor text,
    p_task_id text,
    p_hold_id text,
    p_legs jsonb
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $function$
DECLARE
    v_task public.lab_tasks%ROWTYPE;
    v_hold public.coin_holds%ROWTYPE;
    v_leg jsonb;
    v_recipient text;
    v_delta bigint;
    v_total bigint := 0;
    v_positive_total bigint := 0;
    v_leg_count integer := 0;
    v_required_user_count integer := 0;
    v_user_count integer := 0;
    v_required_treasury_count integer := 0;
    v_treasury_count integer := 0;
    v_expected_transaction_count integer := 0;
    v_persisted_count integer := 0;
    v_canonical_legs jsonb;
    v_request jsonb;
    v_request_digest text;
    v_existing_digest text;
    v_existing_audit_id text;
    v_existing_event_id text;
    v_hash text;
    v_audit_id text;
    v_event_id text;
    v_entry_id text;
    v_transaction_id text;
    v_before bigint;
    v_after bigint;
BEGIN
    IF COALESCE(p_operation_key, '') = ''
       OR p_operation_key <> btrim(p_operation_key)
       OR length(p_operation_key) > 160
       OR COALESCE(p_ticket, '') = ''
       OR p_ticket <> btrim(p_ticket)
       OR length(p_ticket) > 120
       OR COALESCE(p_reason, '') = ''
       OR p_reason <> btrim(p_reason)
       OR length(p_reason) > 2000
       OR COALESCE(p_actor, '') = ''
       OR p_actor <> btrim(p_actor)
       OR length(p_actor) > 160
       OR COALESCE(p_task_id, '') = ''
       OR COALESCE(p_hold_id, '') = '' THEN
        RAISE EXCEPTION 'break-glass compensation metadata is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(p_legs) <> 'array'
       OR jsonb_array_length(p_legs) NOT BETWEEN 2 AND 100 THEN
        RAISE EXCEPTION 'break-glass compensation requires 2 to 100 legs'
            USING ERRCODE = '22023';
    END IF;

    FOR v_leg IN SELECT value FROM jsonb_array_elements(p_legs)
    LOOP
        IF jsonb_typeof(v_leg) <> 'object'
           OR COALESCE(v_leg->>'recipient_key', '') = ''
           OR v_leg->>'recipient_key' <> btrim(v_leg->>'recipient_key')
           OR length(v_leg->>'recipient_key') > 160
           OR jsonb_typeof(v_leg->'amount_delta') <> 'number'
           OR COALESCE(v_leg->>'amount_delta', '') !~ '^-?[1-9][0-9]*$'
           OR (
               v_leg->>'recipient_key' LIKE 'treasury:%'
               AND (
                   length(substring(v_leg->>'recipient_key' FROM 10)) NOT BETWEEN 1 AND 100
                   OR substring(v_leg->>'recipient_key' FROM 10)
                      <> btrim(substring(v_leg->>'recipient_key' FROM 10))
               )
           ) THEN
            RAISE EXCEPTION 'invalid break-glass compensation leg'
                USING ERRCODE = '22023';
        END IF;
        v_delta := (v_leg->>'amount_delta')::bigint;
        IF v_delta NOT BETWEEN -2147483648 AND 2147483647 THEN
            RAISE EXCEPTION 'break-glass compensation delta exceeds integer range'
                USING ERRCODE = '22003';
        END IF;
        v_total := v_total + v_delta;
        IF v_delta > 0 THEN
            v_positive_total := v_positive_total + v_delta;
        END IF;
        v_leg_count := v_leg_count + 1;
    END LOOP;

    IF EXISTS (
        SELECT 1
          FROM jsonb_array_elements(p_legs) AS leg(value)
         GROUP BY leg.value->>'recipient_key'
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'break-glass compensation recipients must be unique'
            USING ERRCODE = '23514';
    END IF;
    IF v_total <> 0 OR v_positive_total <= 0 THEN
        RAISE EXCEPTION 'break-glass compensation must be balanced and nonzero'
            USING ERRCODE = '23514';
    END IF;

    SELECT jsonb_agg(
               jsonb_build_object(
                   'recipient_key', leg.value->>'recipient_key',
                   'amount_delta', (leg.value->>'amount_delta')::bigint
               )
               ORDER BY leg.value->>'recipient_key'
           )
      INTO v_canonical_legs
      FROM jsonb_array_elements(p_legs) AS leg(value);
    v_request := jsonb_build_object(
        'schema', 'simverse.lab.breakglass-compensation.v1',
        'operation_key', p_operation_key,
        'ticket', p_ticket,
        'reason', p_reason,
        'actor', p_actor,
        'task_id', p_task_id,
        'hold_id', p_hold_id,
        'legs', v_canonical_legs
    );
    v_request_digest := encode(
        sha256(convert_to(v_request::text, 'UTF8')),
        'hex'
    );
    v_audit_id := encode(
        sha256(convert_to('audit:' || p_operation_key, 'UTF8')),
        'hex'
    );
    v_hash := encode(
        sha256(convert_to('event:' || p_operation_key, 'UTF8')),
        'hex'
    );
    v_event_id := substring(v_hash FROM 1 FOR 8) || '-'
        || substring(v_hash FROM 9 FOR 4) || '-5'
        || substring(v_hash FROM 14 FOR 3) || '-a'
        || substring(v_hash FROM 18 FOR 3) || '-'
        || substring(v_hash FROM 21 FOR 12);

    -- Serialize exact retries without granting any row-level write capability.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_operation_key, 0)
    );
    SELECT audit_id, request_digest, event_id
      INTO v_existing_audit_id, v_existing_digest, v_existing_event_id
      FROM public.lab_breakglass_audits
     WHERE operation_key = p_operation_key
     FOR UPDATE;
    IF FOUND THEN
        IF v_existing_digest IS DISTINCT FROM v_request_digest
           OR v_existing_audit_id IS DISTINCT FROM v_audit_id
           OR v_existing_event_id IS DISTINCT FROM v_event_id THEN
            RAISE EXCEPTION 'break-glass operation key binding changed'
                USING ERRCODE = '23514';
        END IF;
        SELECT count(*) INTO v_persisted_count
          FROM public.lab_compensation_entries
         WHERE audit_id = v_existing_audit_id;
        IF v_persisted_count <> v_leg_count THEN
            RAISE EXCEPTION 'break-glass compensation ledger is incomplete'
                USING ERRCODE = '23514';
        END IF;
        SELECT count(*) INTO v_expected_transaction_count
          FROM jsonb_array_elements(v_canonical_legs) AS leg(value)
         WHERE leg.value->>'recipient_key' <> 'sink'
           AND leg.value->>'recipient_key' NOT LIKE 'treasury:%';
        SELECT count(*) INTO v_persisted_count
          FROM public.transactions
         WHERE reason = 'lab_compensation:' || v_existing_audit_id;
        IF v_persisted_count <> v_expected_transaction_count
           OR NOT EXISTS (
               SELECT 1 FROM public.outbox_events
                WHERE event_id = v_existing_event_id
                  AND payload_json::jsonb->>'audit_id' = v_existing_audit_id
           ) THEN
            RAISE EXCEPTION 'break-glass compensation effects are incomplete'
                USING ERRCODE = '23514';
        END IF;
        RETURN v_existing_audit_id;
    END IF;

    SELECT * INTO v_task
      FROM public.lab_tasks
     WHERE id = p_task_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'break-glass task not found' USING ERRCODE = '23503';
    END IF;
    SELECT * INTO v_hold
      FROM public.coin_holds
     WHERE id = p_hold_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'break-glass hold not found' USING ERRCODE = '23503';
    END IF;
    IF v_task.hold_id IS DISTINCT FROM v_hold.id
       OR v_hold.user_id IS DISTINCT FROM v_task.issuer_user_id
       OR v_hold.reason IS DISTINCT FROM ('lab_task:' || v_task.id) THEN
        RAISE EXCEPTION 'break-glass task/hold binding mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF v_positive_total > v_hold.amount THEN
        RAISE EXCEPTION 'break-glass gross movement exceeds hold amount'
            USING ERRCODE = '23514';
    END IF;

    PERFORM 1
      FROM public.users
     WHERE id IN (
         SELECT leg.value->>'recipient_key'
           FROM jsonb_array_elements(v_canonical_legs) AS leg(value)
          WHERE leg.value->>'recipient_key' <> 'sink'
            AND leg.value->>'recipient_key' NOT LIKE 'treasury:%'
     )
     ORDER BY id
     FOR UPDATE;
    SELECT count(*) INTO v_required_user_count
      FROM jsonb_array_elements(v_canonical_legs) AS leg(value)
     WHERE leg.value->>'recipient_key' <> 'sink'
       AND leg.value->>'recipient_key' NOT LIKE 'treasury:%';
    SELECT count(*) INTO v_user_count
      FROM public.users
     WHERE id IN (
         SELECT leg.value->>'recipient_key'
           FROM jsonb_array_elements(v_canonical_legs) AS leg(value)
          WHERE leg.value->>'recipient_key' <> 'sink'
            AND leg.value->>'recipient_key' NOT LIKE 'treasury:%'
     );
    IF v_user_count <> v_required_user_count THEN
        RAISE EXCEPTION 'break-glass compensation references a missing user'
            USING ERRCODE = '23503';
    END IF;

    PERFORM 1
      FROM public.resident_treasuries
     WHERE resident_slug IN (
         SELECT substring(leg.value->>'recipient_key' FROM 10)
           FROM jsonb_array_elements(v_canonical_legs) AS leg(value)
          WHERE leg.value->>'recipient_key' LIKE 'treasury:%'
     )
     ORDER BY resident_slug
     FOR UPDATE;
    SELECT count(*) INTO v_required_treasury_count
      FROM jsonb_array_elements(v_canonical_legs) AS leg(value)
     WHERE leg.value->>'recipient_key' LIKE 'treasury:%';
    SELECT count(*) INTO v_treasury_count
      FROM public.resident_treasuries
     WHERE resident_slug IN (
         SELECT substring(leg.value->>'recipient_key' FROM 10)
           FROM jsonb_array_elements(v_canonical_legs) AS leg(value)
          WHERE leg.value->>'recipient_key' LIKE 'treasury:%'
     );
    IF v_treasury_count <> v_required_treasury_count THEN
        RAISE EXCEPTION 'break-glass compensation references a missing treasury'
            USING ERRCODE = '23503';
    END IF;

    -- Validate every resulting real-account balance before the first mutation.
    FOR v_leg IN SELECT value FROM jsonb_array_elements(v_canonical_legs)
    LOOP
        v_recipient := v_leg->>'recipient_key';
        v_delta := (v_leg->>'amount_delta')::bigint;
        IF v_recipient = 'sink' THEN
            CONTINUE;
        ELSIF v_recipient LIKE 'treasury:%' THEN
            SELECT balance_sc INTO v_before
              FROM public.resident_treasuries
             WHERE resident_slug = substring(v_recipient FROM 10);
        ELSE
            SELECT soul_coin_balance INTO v_before
              FROM public.users
             WHERE id = v_recipient;
        END IF;
        v_after := v_before + v_delta;
        IF v_after NOT BETWEEN 0 AND 2147483647 THEN
            RAISE EXCEPTION 'break-glass compensation would create an invalid balance'
                USING ERRCODE = '23514';
        END IF;
    END LOOP;

    INSERT INTO public.lab_breakglass_audits(
        audit_id, ticket, reason, actor, operation, task_id, hold_id,
        amount, operation_key, event_id, request_digest, payload_json, created_at
    ) VALUES (
        v_audit_id, p_ticket, p_reason, p_actor, 'balanced_adjustment',
        p_task_id, p_hold_id, v_positive_total, p_operation_key,
        v_event_id, v_request_digest, v_request, pg_catalog.clock_timestamp()
    );
    PERFORM public.lab_terminalization_checkpoint('breakglass:after_audit');

    FOR v_leg IN SELECT value FROM jsonb_array_elements(v_canonical_legs)
    LOOP
        v_recipient := v_leg->>'recipient_key';
        v_delta := (v_leg->>'amount_delta')::bigint;
        v_before := NULL;
        v_after := NULL;
        IF v_recipient = 'sink' THEN
            NULL;
        ELSIF v_recipient LIKE 'treasury:%' THEN
            SELECT balance_sc INTO v_before
              FROM public.resident_treasuries
             WHERE resident_slug = substring(v_recipient FROM 10);
            v_after := v_before + v_delta;
            UPDATE public.resident_treasuries
               SET balance_sc = v_after,
                   updated_at = pg_catalog.clock_timestamp()
             WHERE resident_slug = substring(v_recipient FROM 10);
        ELSE
            SELECT soul_coin_balance INTO v_before
              FROM public.users
             WHERE id = v_recipient;
            v_after := v_before + v_delta;
            UPDATE public.users
               SET soul_coin_balance = v_after
             WHERE id = v_recipient;
            v_transaction_id := encode(
                sha256(convert_to(
                    'transaction:' || p_operation_key || ':' || v_recipient,
                    'UTF8'
                )),
                'hex'
            );
            INSERT INTO public.transactions(id, user_id, amount, reason, created_at)
            VALUES (
                v_transaction_id,
                v_recipient,
                v_delta,
                'lab_compensation:' || v_audit_id,
                pg_catalog.clock_timestamp()
            );
        END IF;
        PERFORM public.lab_terminalization_checkpoint(
            'breakglass:after_balance:' || v_recipient
        );

        v_entry_id := encode(
            sha256(convert_to(
                'entry:' || p_operation_key || ':' || v_recipient,
                'UTF8'
            )),
            'hex'
        );
        INSERT INTO public.lab_compensation_entries(
            entry_id, audit_id, task_id, hold_id, recipient_key,
            amount_delta, operation_key, reason, account_balance_before,
            account_balance_after, created_at
        ) VALUES (
            v_entry_id, v_audit_id, p_task_id, p_hold_id, v_recipient,
            v_delta, v_entry_id, p_reason, v_before, v_after,
            pg_catalog.clock_timestamp()
        );
        PERFORM public.lab_terminalization_checkpoint(
            'breakglass:after_ledger:' || v_recipient
        );
    END LOOP;

    INSERT INTO public.outbox_events(
        event_id, tenant_id, run_id, topic, payload_json, created_at
    ) VALUES (
        v_event_id,
        v_task.issuer_user_id,
        v_task.accepted_run_id,
        'lab_run_event',
        jsonb_build_object(
            'type', 'lab.finance.compensated',
            'schema_version', 1,
            'event_id', v_event_id,
            'audit_id', v_audit_id,
            'operation_key', p_operation_key,
            'task_id', p_task_id,
            'hold_id', p_hold_id,
            'gross_amount', v_positive_total,
            'request_digest', v_request_digest
        ),
        pg_catalog.clock_timestamp()
    );
    PERFORM public.lab_terminalization_checkpoint('breakglass:after_outbox');
    PERFORM public.lab_terminalization_checkpoint('breakglass:before_commit');
    RETURN v_audit_id;
END
$function$;
"""


def _postgresql_upgrade() -> None:
    op.execute(
        r"""
        DO $roles$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lab_financial_kernel_owner') THEN
                CREATE ROLE lab_financial_kernel_owner NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lab_terminalizer_v2') THEN
                CREATE ROLE lab_terminalizer_v2 LOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lab_command_submitter_v2') THEN
                CREATE ROLE lab_command_submitter_v2 NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lab_terminalizer_breakglass') THEN
                CREATE ROLE lab_terminalizer_breakglass NOLOGIN;
            END IF;
        END
        $roles$
        """
    )
    for statement in (
        "ALTER ROLE lab_financial_kernel_owner NOLOGIN NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOINHERIT NOREPLICATION",
        "ALTER ROLE lab_terminalizer_v2 LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOINHERIT NOREPLICATION",
        "ALTER ROLE lab_command_submitter_v2 NOLOGIN NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOINHERIT NOREPLICATION",
        "ALTER ROLE lab_terminalizer_breakglass NOLOGIN NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOINHERIT NOREPLICATION",
        "REVOKE lab_financial_kernel_owner FROM lab_terminalizer_v2",
        "REVOKE lab_financial_kernel_owner FROM lab_command_submitter_v2",
        "REVOKE lab_financial_kernel_owner FROM lab_terminalizer_breakglass",
    ):
        op.execute(statement)

    op.execute(CHECKPOINT_FUNCTION_SQL)
    op.execute(STABLE_ID_FUNCTION_SQL)
    op.execute(CANONICAL_PAYLOAD_FUNCTION_SQL)
    op.execute(SUBMIT_FUNCTION_SQL)
    op.execute(GUARD_FUNCTION_SQL)
    op.execute(FINALIZE_FUNCTION_SQL)
    op.execute(APPEND_ONLY_FUNCTION_SQL)
    op.execute(BREAKGLASS_FUNCTION_SQL)
    statements = (
        "ALTER FUNCTION public.lab_terminalization_checkpoint(text) OWNER TO lab_financial_kernel_owner",
        "ALTER FUNCTION public.lab_terminalization_stable_id(text, text) "
        "OWNER TO lab_financial_kernel_owner",
        "ALTER FUNCTION public.canonical_lab_terminalization_payload(text, text, text, bigint) "
        "OWNER TO lab_financial_kernel_owner",
        "ALTER FUNCTION public.submit_lab_terminalization_command(text, text, text, bigint) "
        "OWNER TO lab_financial_kernel_owner",
        "ALTER FUNCTION public.guard_lab_terminal_mutation() OWNER TO lab_financial_kernel_owner",
        "ALTER FUNCTION public.finalize_lab_terminalization(text, bigint) OWNER TO lab_financial_kernel_owner",
        "ALTER FUNCTION public.reject_lab_compensation_history_mutation() "
        "OWNER TO lab_financial_kernel_owner",
        "ALTER FUNCTION public.apply_lab_breakglass_compensation(text, text, text, "
        "text, text, text, jsonb) OWNER TO lab_financial_kernel_owner",
        "REVOKE ALL ON FUNCTION public.lab_terminalization_checkpoint(text) FROM PUBLIC, "
        "lab_command_submitter_v2, lab_terminalizer_v2, lab_terminalizer_breakglass",
        "REVOKE ALL ON FUNCTION public.lab_terminalization_stable_id(text, text) "
        "FROM PUBLIC, lab_command_submitter_v2, lab_terminalizer_v2, "
        "lab_terminalizer_breakglass",
        "REVOKE ALL ON FUNCTION public.canonical_lab_terminalization_payload(text, "
        "text, text, bigint) FROM PUBLIC, lab_command_submitter_v2, "
        "lab_terminalizer_v2, lab_terminalizer_breakglass",
        "REVOKE ALL ON FUNCTION public.submit_lab_terminalization_command(text, text, "
        "text, bigint) FROM PUBLIC, lab_command_submitter_v2, "
        "lab_terminalizer_v2, lab_terminalizer_breakglass",
        "REVOKE ALL ON FUNCTION public.guard_lab_terminal_mutation() FROM PUBLIC",
        "REVOKE ALL ON FUNCTION public.finalize_lab_terminalization(text, bigint) FROM PUBLIC",
        "REVOKE ALL ON FUNCTION public.reject_lab_compensation_history_mutation() "
        "FROM PUBLIC, lab_terminalizer_v2, lab_terminalizer_breakglass",
        "REVOKE ALL ON FUNCTION public.apply_lab_breakglass_compensation(text, text, "
        "text, text, text, text, jsonb) FROM PUBLIC",
        "REVOKE ALL ON FUNCTION public.finalize_lab_terminalization(text, bigint) "
        "FROM lab_terminalizer_breakglass",
        "REVOKE ALL ON FUNCTION public.apply_lab_breakglass_compensation(text, text, "
        "text, text, text, text, jsonb) FROM lab_terminalizer_v2, "
        "lab_terminalizer_breakglass",
        "GRANT USAGE ON SCHEMA public TO lab_command_submitter_v2, "
        "lab_terminalizer_v2, lab_financial_kernel_owner",
        "GRANT EXECUTE ON FUNCTION public.submit_lab_terminalization_command(text, text, "
        "text, bigint) TO lab_command_submitter_v2",
        "GRANT EXECUTE ON FUNCTION public.finalize_lab_terminalization(text, bigint) "
        "TO lab_terminalizer_v2",
        "REVOKE ALL ON public.coin_hold_entries, public.lab_terminalization_commands, "
        "public.lab_terminalization_receipts, public.lab_breakglass_audits, "
        "public.lab_compensation_entries FROM PUBLIC, lab_command_submitter_v2, "
        "lab_terminalizer_v2, lab_terminalizer_breakglass",
        "REVOKE UPDATE(status, settled_at, terminalization_version, cutover_at) ON "
        "public.coin_holds FROM PUBLIC, lab_command_submitter_v2, lab_terminalizer_v2, "
        "lab_terminalizer_breakglass",
        "REVOKE UPDATE(status, completed_at, updated_at) ON public.lab_tasks FROM "
        "PUBLIC, lab_command_submitter_v2, lab_terminalizer_v2, "
        "lab_terminalizer_breakglass",
        "REVOKE UPDATE(soul_coin_balance) ON public.users FROM PUBLIC, "
        "lab_command_submitter_v2, lab_terminalizer_v2, lab_terminalizer_breakglass",
        "REVOKE ALL ON public.transactions, public.resident_treasuries, "
        "public.outbox_events FROM lab_command_submitter_v2, lab_terminalizer_v2, "
        "lab_terminalizer_breakglass",
        "ALTER TABLE public.coin_hold_entries OWNER TO lab_financial_kernel_owner",
        "ALTER TABLE public.lab_terminalization_commands OWNER TO lab_financial_kernel_owner",
        "ALTER TABLE public.lab_terminalization_receipts OWNER TO lab_financial_kernel_owner",
        "ALTER TABLE public.lab_breakglass_audits OWNER TO lab_financial_kernel_owner",
        "ALTER TABLE public.lab_compensation_entries OWNER TO lab_financial_kernel_owner",
        "GRANT SELECT, UPDATE ON public.lab_terminalization_commands TO lab_financial_kernel_owner",
        "GRANT SELECT, UPDATE ON public.coin_holds, public.lab_tasks, public.users, "
        "public.resident_treasuries, public.lab_runs, public.lab_run_leases "
        "TO lab_financial_kernel_owner",
        "GRANT SELECT ON public.residents TO lab_financial_kernel_owner",
        "GRANT INSERT, SELECT ON public.transactions, public.coin_hold_entries, "
        "public.outbox_events, public.lab_terminalization_receipts, "
        "public.lab_breakglass_audits, public.lab_compensation_entries "
        "TO lab_financial_kernel_owner",
        "GRANT INSERT ON public.resident_treasuries TO lab_financial_kernel_owner",
        "GRANT USAGE, SELECT ON SEQUENCE public.outbox_events_id_seq "
        "TO lab_financial_kernel_owner",
        "DROP TRIGGER IF EXISTS trg_guard_coin_hold_terminal_mutation ON public.coin_holds",
        r"""CREATE TRIGGER trg_guard_coin_hold_terminal_mutation
        BEFORE UPDATE OF status, settled_at, terminalization_version, cutover_at
        ON public.coin_holds
        FOR EACH ROW
        WHEN (
            OLD.status IS DISTINCT FROM NEW.status
            OR OLD.settled_at IS DISTINCT FROM NEW.settled_at
            OR OLD.terminalization_version IS DISTINCT FROM NEW.terminalization_version
            OR OLD.cutover_at IS DISTINCT FROM NEW.cutover_at
        )
        EXECUTE FUNCTION public.guard_lab_terminal_mutation()""",
        "DROP TRIGGER IF EXISTS trg_guard_lab_task_terminal_mutation ON public.lab_tasks",
        r"""CREATE TRIGGER trg_guard_lab_task_terminal_mutation
        BEFORE UPDATE OF status, completed_at, terminal_creator_share_bps
        ON public.lab_tasks
        FOR EACH ROW
        WHEN (
            OLD.terminal_creator_share_bps IS DISTINCT FROM
                NEW.terminal_creator_share_bps
            OR (
                (OLD.status IN ('completed', 'failed', 'expired', 'cancelled')
                 OR NEW.status IN ('completed', 'failed', 'expired', 'cancelled'))
                AND (OLD.status IS DISTINCT FROM NEW.status
                     OR OLD.completed_at IS DISTINCT FROM NEW.completed_at)
            )
        )
        EXECUTE FUNCTION public.guard_lab_terminal_mutation()""",
        "DROP TRIGGER IF EXISTS trg_reject_lab_breakglass_audit_mutation "
        "ON public.lab_breakglass_audits",
        r"""CREATE TRIGGER trg_reject_lab_breakglass_audit_mutation
        BEFORE UPDATE OR DELETE ON public.lab_breakglass_audits
        FOR EACH ROW
        EXECUTE FUNCTION public.reject_lab_compensation_history_mutation()""",
        "DROP TRIGGER IF EXISTS trg_reject_lab_compensation_entry_mutation "
        "ON public.lab_compensation_entries",
        r"""CREATE TRIGGER trg_reject_lab_compensation_entry_mutation
        BEFORE UPDATE OR DELETE ON public.lab_compensation_entries
        FOR EACH ROW
        EXECUTE FUNCTION public.reject_lab_compensation_history_mutation()""",
    )
    op.execute(
        r"""DO $grant_connect$
        BEGIN
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO lab_terminalizer_v2, lab_command_submitter_v2',
                current_database()
            );
        END
        $grant_connect$"""
    )
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    op.add_column(
        "lab_tasks",
        sa.Column("terminal_creator_share_bps", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_lab_tasks_terminal_creator_share_bps",
        "lab_tasks",
        "terminal_creator_share_bps IS NULL OR "
        "terminal_creator_share_bps BETWEEN 0 AND 10000",
    )
    op.add_column(
        "coin_holds",
        sa.Column(
            "terminalization_version",
            sa.String(length=2),
            nullable=False,
            server_default="v1",
        ),
    )
    op.add_column(
        "coin_holds", sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_coin_holds_amount_positive", "coin_holds", "amount > 0"
    )
    op.create_check_constraint(
        "ck_coin_holds_status", "coin_holds", "status IN ('held', 'settled', 'refunded')"
    )
    op.create_check_constraint(
        "ck_coin_holds_terminalization_version",
        "coin_holds",
        "terminalization_version IN ('v1', 'v2')",
    )
    op.create_check_constraint(
        "ck_coin_holds_v2_cutover",
        "coin_holds",
        "terminalization_version = 'v1' OR cutover_at IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_coin_holds_terminal_timestamp",
        "coin_holds",
        "(status = 'held' AND settled_at IS NULL) OR "
        "(status IN ('settled', 'refunded') AND settled_at IS NOT NULL)",
    )
    op.create_index(
        "ix_coin_holds_terminalization_version",
        "coin_holds",
        ["terminalization_version"],
    )

    op.create_table(
        "coin_hold_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "hold_id",
            sa.String(),
            sa.ForeignKey("coin_holds.id"),
            nullable=False,
        ),
        sa.Column("terminal_action", sa.String(length=20), nullable=False),
        sa.Column("recipient_key", sa.String(length=160), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("operation_key", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_coin_hold_entries_amount_positive"),
        sa.CheckConstraint(
            "terminal_action IN ('settle', 'refund')",
            name="ck_coin_hold_entries_terminal_action",
        ),
        sa.UniqueConstraint("operation_key", name="uq_coin_hold_entries_operation_key"),
        sa.UniqueConstraint(
            "hold_id",
            "terminal_action",
            "recipient_key",
            name="uq_coin_hold_entries_terminal_recipient",
        ),
    )
    op.create_index("ix_coin_hold_entries_hold_id", "coin_hold_entries", ["hold_id"])

    op.create_table(
        "lab_terminalization_commands",
        sa.Column("command_id", sa.String(length=64), primary_key=True),
        sa.Column("operation", sa.String(length=24), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("hold_id", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("expected_epoch", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("expected_epoch >= 0", name="ck_lab_terminalization_epoch"),
        sa.CheckConstraint(
            "operation IN ('accept', 'auto_release', 'arbitrate_settle', "
            "'arbitrate_refund', 'fail', 'cancel', 'expire')",
            name="ck_lab_terminalization_command_operation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_lab_terminalization_command_status",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_lab_terminalization_idempotency"
        ),
        sa.UniqueConstraint(
            "operation",
            "task_id",
            "hold_id",
            "actor",
            "expected_epoch",
            name="uq_lab_terminalization_command_identity",
        ),
    )
    op.create_index(
        "ix_lab_terminalization_commands_operation",
        "lab_terminalization_commands",
        ["operation"],
    )
    op.create_index(
        "ix_lab_terminalization_commands_status",
        "lab_terminalization_commands",
        ["status"],
    )
    op.create_index(
        "ix_lab_terminalization_commands_task_id",
        "lab_terminalization_commands",
        ["task_id"],
    )
    op.create_index(
        "ix_lab_terminalization_commands_hold_id",
        "lab_terminalization_commands",
        ["hold_id"],
    )
    op.create_index(
        "ix_lab_terminalization_commands_actor",
        "lab_terminalization_commands",
        ["actor"],
    )

    op.create_table(
        "lab_terminalization_receipts",
        sa.Column("receipt_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "command_id",
            sa.String(length=64),
            sa.ForeignKey("lab_terminalization_commands.command_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("hold_id", sa.String(), nullable=False),
        sa.Column("operation", sa.String(length=24), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("journal_count", sa.Integer(), nullable=False),
        sa.Column("result_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_lab_terminalization_receipt_amount"),
        sa.CheckConstraint(
            "journal_count >= 0", name="ck_lab_terminalization_receipt_journal_count"
        ),
        sa.CheckConstraint(
            "length(result_digest) = 64",
            name="ck_lab_terminalization_receipt_digest",
        ),
        sa.UniqueConstraint("command_id", name="uq_lab_terminalization_receipt_command"),
        sa.UniqueConstraint("event_id", name="uq_lab_terminalization_receipt_event"),
    )
    op.create_index(
        "ix_lab_terminalization_receipts_task_id",
        "lab_terminalization_receipts",
        ["task_id"],
    )
    op.create_index(
        "ix_lab_terminalization_receipts_hold_id",
        "lab_terminalization_receipts",
        ["hold_id"],
    )

    op.create_table(
        "lab_breakglass_audits",
        sa.Column("audit_id", sa.String(length=64), primary_key=True),
        sa.Column("ticket", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("operation", sa.String(length=24), nullable=False),
        sa.Column(
            "task_id",
            sa.String(),
            sa.ForeignKey("lab_tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "hold_id",
            sa.String(),
            sa.ForeignKey("coin_holds.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("operation_key", sa.String(length=160), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_lab_breakglass_audit_amount"),
        sa.CheckConstraint(
            "operation = 'balanced_adjustment'",
            name="ck_lab_breakglass_audit_operation",
        ),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name="ck_lab_breakglass_audit_digest",
        ),
        sa.UniqueConstraint("operation_key", name="uq_lab_breakglass_audit_operation"),
        sa.UniqueConstraint("event_id", name="uq_lab_breakglass_audit_event"),
    )

    op.create_table(
        "lab_compensation_entries",
        sa.Column("entry_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "audit_id",
            sa.String(length=64),
            sa.ForeignKey("lab_breakglass_audits.audit_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("hold_id", sa.String(), nullable=False),
        sa.Column("recipient_key", sa.String(length=160), nullable=False),
        sa.Column("amount_delta", sa.Integer(), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("account_balance_before", sa.Integer(), nullable=True),
        sa.Column("account_balance_after", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount_delta <> 0",
            name="ck_lab_compensation_entry_delta_nonzero",
        ),
        sa.CheckConstraint(
            "(recipient_key = 'sink' AND account_balance_before IS NULL "
            "AND account_balance_after IS NULL) OR "
            "(recipient_key <> 'sink' AND account_balance_before IS NOT NULL "
            "AND account_balance_after IS NOT NULL "
            "AND account_balance_before >= 0 AND account_balance_after >= 0 "
            "AND account_balance_after = account_balance_before + amount_delta)",
            name="ck_lab_compensation_entry_balance",
        ),
        sa.UniqueConstraint(
            "operation_key",
            name="uq_lab_compensation_entry_operation",
        ),
        sa.UniqueConstraint(
            "audit_id",
            "recipient_key",
            name="uq_lab_compensation_entry_recipient",
        ),
    )

    if op.get_bind().dialect.name == "postgresql":
        _postgresql_upgrade()


def downgrade() -> None:
    # Block producers before inspecting history so no commit can land between
    # the guard query and the destructive DDL in this migration transaction.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "LOCK TABLE coin_holds, lab_terminalization_commands, "
                "lab_terminalization_receipts, coin_hold_entries, "
                "lab_breakglass_audits, lab_compensation_entries "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    history = bind.execute(
        sa.text(
            "SELECT "
            "(SELECT count(*) FROM coin_holds "
            " WHERE terminalization_version = 'v2') AS v2_holds, "
            "(SELECT count(*) FROM lab_terminalization_commands) AS commands, "
            "(SELECT count(*) FROM lab_terminalization_receipts) AS receipts, "
            "(SELECT count(*) FROM coin_hold_entries) AS journal_entries, "
            "(SELECT count(*) FROM lab_breakglass_audits) AS breakglass_audits, "
            "(SELECT count(*) FROM lab_compensation_entries) AS compensation_entries"
        )
    ).mappings().one()
    history_counts = {key: int(value) for key, value in history.items()}
    if any(history_counts.values()):
        details = ", ".join(
            f"{key}={value}" for key, value in history_counts.items()
        )
        raise RuntimeError(
            f"refusing Lab terminalization downgrade: durable history exists ({details})"
        )

    if bind.dialect.name == "postgresql":
        for statement in (
            "DROP TRIGGER IF EXISTS trg_guard_lab_task_terminal_mutation ON public.lab_tasks",
            "DROP TRIGGER IF EXISTS trg_guard_coin_hold_terminal_mutation ON public.coin_holds",
            "DROP TRIGGER IF EXISTS trg_reject_lab_breakglass_audit_mutation "
            "ON public.lab_breakglass_audits",
            "DROP TRIGGER IF EXISTS trg_reject_lab_compensation_entry_mutation "
            "ON public.lab_compensation_entries",
            "DROP FUNCTION IF EXISTS public.apply_lab_breakglass_compensation(text, "
            "text, text, text, text, text, jsonb)",
            "DROP FUNCTION IF EXISTS public.reject_lab_compensation_history_mutation()",
            "DROP FUNCTION IF EXISTS public.submit_lab_terminalization_command(text, "
            "text, text, bigint)",
            "DROP FUNCTION IF EXISTS public.finalize_lab_terminalization(text, bigint)",
            "DROP FUNCTION IF EXISTS public.canonical_lab_terminalization_payload(text, "
            "text, text, bigint)",
            "DROP FUNCTION IF EXISTS public.lab_terminalization_stable_id(text, text)",
            "DROP FUNCTION IF EXISTS public.guard_lab_terminal_mutation()",
            "DROP FUNCTION IF EXISTS public.lab_terminalization_checkpoint(text)",
        ):
            op.execute(statement)

    op.drop_table("lab_compensation_entries")
    op.drop_table("lab_breakglass_audits")
    op.drop_index(
        "ix_lab_terminalization_receipts_hold_id",
        table_name="lab_terminalization_receipts",
    )
    op.drop_index(
        "ix_lab_terminalization_receipts_task_id",
        table_name="lab_terminalization_receipts",
    )
    op.drop_table("lab_terminalization_receipts")
    op.drop_index(
        "ix_lab_terminalization_commands_actor",
        table_name="lab_terminalization_commands",
    )
    op.drop_index(
        "ix_lab_terminalization_commands_hold_id",
        table_name="lab_terminalization_commands",
    )
    op.drop_index(
        "ix_lab_terminalization_commands_task_id",
        table_name="lab_terminalization_commands",
    )
    op.drop_index(
        "ix_lab_terminalization_commands_status",
        table_name="lab_terminalization_commands",
    )
    op.drop_index(
        "ix_lab_terminalization_commands_operation",
        table_name="lab_terminalization_commands",
    )
    op.drop_table("lab_terminalization_commands")
    op.drop_index("ix_coin_hold_entries_hold_id", table_name="coin_hold_entries")
    op.drop_table("coin_hold_entries")
    op.drop_index(
        "ix_coin_holds_terminalization_version", table_name="coin_holds"
    )
    op.drop_constraint("ck_coin_holds_terminal_timestamp", "coin_holds", type_="check")
    op.drop_constraint("ck_coin_holds_v2_cutover", "coin_holds", type_="check")
    op.drop_constraint(
        "ck_coin_holds_terminalization_version", "coin_holds", type_="check"
    )
    op.drop_constraint("ck_coin_holds_status", "coin_holds", type_="check")
    op.drop_constraint("ck_coin_holds_amount_positive", "coin_holds", type_="check")
    op.drop_column("coin_holds", "cutover_at")
    op.drop_column("coin_holds", "terminalization_version")
    op.drop_constraint(
        "ck_lab_tasks_terminal_creator_share_bps", "lab_tasks", type_="check"
    )
    op.drop_column("lab_tasks", "terminal_creator_share_bps")
