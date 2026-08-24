"""Durable state, secret envelopes and admin projections for hosted Agents."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import SecretStr
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.agent_player import AgentCredential, AgentPlayer
from app.models.hosted_agent import (
    HostedAgentController,
    HostedAgentDailyUsage,
    HostedAgentTurn,
)
from app.models.resident import Resident
from app.models.user import User
from app.services.agent_player_service import (
    AgentPlayerError,
    register_agent_player,
    redeem_pairing,
    viewer_snapshot,
)
from app.services.hosted_agent_runner_crypto import HostedRunnerSecretError


HOSTED_CONTROLLER_SECRET_FIELD = "secret_bundle"
HOSTED_OBSERVATION_FIELD = "observation"
HOSTED_DECISION_FIELD = "decision"
HOSTED_RESULT_FIELD = "result"
HOSTED_LEASE_STATES = frozenset(
    {"provisioning", "idle", "claimed", "backoff", "budget_paused", "auth_blocked", "error"}
)


class HostedAgentError(RuntimeError):
    def __init__(self, code: str, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hosted_identity_slug(controller_id: str) -> str:
    try:
        stable = uuid.UUID(str(controller_id)).hex
    except (TypeError, ValueError, AttributeError):
        stable = "".join(ch for ch in str(controller_id).lower() if ch.isalnum())[:32]
        if not stable:
            stable = uuid.uuid5(uuid.NAMESPACE_DNS, "simverse-hosted-agent").hex
    return f"p-hosted-{stable}"


def create_request_hash(payload: dict[str, Any]) -> str:
    """Keyed idempotency digest; private fields never appear in the DB."""
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        ("hosted-agent-create:v1:" + _canonical_json(payload)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def encrypt_secret_bundle(controller_id: str, bundle: dict[str, Any]) -> str:
    keyring = settings.hosted_agent_runner_secret_keyring
    if keyring is None:
        raise HostedAgentError("hosted_runner_disabled", "Hosted Agent runner is disabled", 404)
    return keyring.encrypt_secret(
        SecretStr(_canonical_json(bundle)),
        row_ref=f"hosted_agent_controllers:{controller_id}",
        field_name=HOSTED_CONTROLLER_SECRET_FIELD,
    )


def decrypt_secret_bundle(controller: HostedAgentController) -> dict[str, Any]:
    keyring = settings.hosted_agent_runner_secret_keyring
    if keyring is None:
        raise HostedAgentError("hosted_runner_disabled", "Hosted Agent runner is disabled", 404)
    try:
        raw = keyring.decrypt_secret(
            controller.secret_envelope,
            row_ref=f"hosted_agent_controllers:{controller.id}",
            field_name=HOSTED_CONTROLLER_SECRET_FIELD,
        ).get_secret_value()
        parsed = json.loads(raw)
    except (HostedRunnerSecretError, ValueError, TypeError) as exc:
        raise HostedAgentError(
            "secret_unavailable", "Hosted Agent credential envelope is unavailable", 503
        ) from exc
    if not isinstance(parsed, dict):
        raise HostedAgentError(
            "secret_unavailable", "Hosted Agent credential envelope is unavailable", 503
        )
    return parsed


def encrypt_turn_value(
    *, turn_id: str, field_name: str, value: dict[str, Any]
) -> str:
    if field_name not in {
        HOSTED_OBSERVATION_FIELD,
        HOSTED_DECISION_FIELD,
        HOSTED_RESULT_FIELD,
    }:
        raise ValueError("unsupported hosted turn secret field")
    keyring = settings.hosted_agent_runner_secret_keyring
    if keyring is None:
        raise HostedAgentError("hosted_runner_disabled", "Hosted Agent runner is disabled", 404)
    return keyring.encrypt_secret(
        SecretStr(_canonical_json(value)),
        row_ref=f"hosted_agent_turns:{turn_id}",
        field_name=field_name,
    )


def decrypt_turn_value(turn: HostedAgentTurn, field_name: str) -> dict[str, Any]:
    envelope = {
        HOSTED_OBSERVATION_FIELD: turn.observation_envelope,
        HOSTED_DECISION_FIELD: turn.decision_envelope,
        HOSTED_RESULT_FIELD: turn.result_envelope,
    }.get(field_name)
    if not envelope:
        raise HostedAgentError("turn_secret_unavailable", "Hosted Agent turn is incomplete", 409)
    keyring = settings.hosted_agent_runner_secret_keyring
    if keyring is None:
        raise HostedAgentError("hosted_runner_disabled", "Hosted Agent runner is disabled", 404)
    try:
        raw = keyring.decrypt_secret(
            envelope,
            row_ref=f"hosted_agent_turns:{turn.id}",
            field_name=field_name,
        ).get_secret_value()
        parsed = json.loads(raw)
    except (HostedRunnerSecretError, ValueError, TypeError) as exc:
        raise HostedAgentError(
            "turn_secret_unavailable", "Hosted Agent turn envelope is unavailable", 503
        ) from exc
    if not isinstance(parsed, dict):
        raise HostedAgentError(
            "turn_secret_unavailable", "Hosted Agent turn envelope is unavailable", 503
        )
    return parsed


async def create_controller(
    db: AsyncSession,
    *,
    owner_user_id: str,
    request_id: str,
    request_hash: str,
    provider_host: str,
    model: str,
    bundle: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[HostedAgentController, bool]:
    existing = (
        await db.execute(
            select(HostedAgentController).where(
                HostedAgentController.owner_user_id == owner_user_id,
                HostedAgentController.request_id == request_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not hmac.compare_digest(existing.create_request_hash, request_hash):
            raise HostedAgentError(
                "idempotency_conflict",
                "request_id was already used with different input",
                409,
            )
        return existing, False

    for capacity_slot in range(settings.hosted_agent_runner_max_concurrent):
        controller_id = str(uuid.uuid4())
        controller = HostedAgentController(
            id=controller_id,
            owner_user_id=owner_user_id,
            request_id=request_id,
            create_request_hash=request_hash,
            desired_status="running",
            runtime_status="provisioning",
            control_version=1,
            capacity_slot=capacity_slot,
            provider_host=provider_host,
            model=model,
            provider_validation_required=True,
            secret_version=1,
            secret_envelope=encrypt_secret_bundle(controller_id, bundle),
            identity_json={
                "display_name": bundle.get("display_name"),
                "goals": {"public": bundle.get("public_goal")},
            },
            policy_json={"identity_frame_version": "hosted-v1", **policy},
            heartbeat_seconds=policy["heartbeat_seconds"],
            action_interval_seconds=policy["action_interval_seconds"],
            max_actions_per_day=policy["daily_action_limit"],
            max_provider_calls_per_day=policy.get(
                "daily_provider_call_limit",
                min(2000, policy["daily_action_limit"] * 2),
            ),
            max_tokens_per_day=policy["daily_token_limit"],
            max_output_tokens=policy.get("max_output_tokens", 600),
            next_tick_at=_now(),
            next_action_at=_now(),
        )
        db.add(controller)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raced = (
                await db.execute(
                    select(HostedAgentController).where(
                        HostedAgentController.owner_user_id == owner_user_id,
                        HostedAgentController.request_id == request_id,
                    )
                )
            ).scalar_one_or_none()
            if raced is not None:
                if not hmac.compare_digest(raced.create_request_hash, request_hash):
                    raise HostedAgentError(
                        "idempotency_conflict",
                        "request_id was already used with different input",
                        409,
                    )
                return raced, False
            continue
        await db.refresh(controller)
        return controller, True
    raise HostedAgentError(
        "hosted_agent_capacity_exhausted",
        "All Hosted Agent runtime slots are in use",
        409,
    )


async def owner_controller(
    db: AsyncSession, *, controller_id: str, owner_user_id: str, for_update: bool = False
) -> HostedAgentController:
    query = select(HostedAgentController).where(
        HostedAgentController.id == controller_id,
        HostedAgentController.owner_user_id == owner_user_id,
    )
    if for_update:
        query = query.with_for_update()
    controller = (await db.execute(query)).scalar_one_or_none()
    if controller is None:
        raise HostedAgentError("hosted_agent_not_found", "Hosted Agent not found", 404)
    return controller


def controller_public(controller: HostedAgentController) -> dict[str, Any]:
    identity = controller.identity_json if isinstance(controller.identity_json, dict) else {}
    public_goal = (
        identity.get("goals", {}).get("public")
        if isinstance(identity.get("goals"), dict)
        else None
    )
    return {
        "id": controller.id,
        "request_id": controller.request_id,
        "desired_status": controller.desired_status,
        "runtime_status": controller.runtime_status,
        "version": controller.control_version,
        "display_name": identity.get("display_name"),
        "resident_slug": identity.get("slug"),
        "goal": public_goal,
        "agent": (
            {
                "id": controller.agent_player_id,
                "name": identity.get("display_name"),
                "slug": identity.get("slug"),
                "introduction": identity.get("introduction"),
            }
            if controller.agent_player_id
            else None
        ),
        "provider": {
            "host": controller.provider_host,
            "model": controller.model,
            "key_configured": True,
        },
        "policy": {
            "heartbeat_seconds": controller.heartbeat_seconds,
            "action_interval_seconds": controller.action_interval_seconds,
            "daily_action_limit": controller.max_actions_per_day,
            "daily_token_limit": controller.max_tokens_per_day,
            "max_output_tokens": controller.max_output_tokens,
        },
        "health": {
            "last_heartbeat_at": controller.heartbeat_at,
            "last_presence_at": controller.last_presence_at,
            "last_action_at": controller.last_action_at,
            "next_retry_at": controller.provider_retry_at or controller.next_tick_at,
            "consecutive_failures": controller.retry_count,
        },
        "last_error_code": controller.last_error_code,
        "created_at": controller.created_at,
        "updated_at": controller.updated_at,
    }


def _safe_public_turn_summary(turn: HostedAgentTurn) -> str:
    if isinstance(turn.public_summary, str) and turn.public_summary.strip():
        return turn.public_summary.strip()[:280]
    if turn.state == "failed":
        return "本轮行动未能完成"
    if turn.state == "abandoned":
        return "本轮行动已取消"
    if turn.state == "completed":
        return "完成了一次小镇行动"
    return "正在准备下一步小镇行动"


async def controller_state(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    after_log_seq: int = 0,
) -> dict[str, Any]:
    usage = await db.get(HostedAgentDailyUsage, (controller.id, _now().date()))
    turns = (
        await db.execute(
            select(HostedAgentTurn)
            .where(
                HostedAgentTurn.controller_id == controller.id,
                HostedAgentTurn.sequence > after_log_seq,
            )
            .order_by(HostedAgentTurn.sequence)
            .limit(200)
        )
    ).scalars().all()
    snapshot = None
    if controller.agent_player_id:
        profile = await db.get(AgentPlayer, controller.agent_player_id)
        if profile is not None:
            snapshot = await viewer_snapshot(db, profile)
    data = controller_public(controller)
    data.update(
        {
            "snapshot": snapshot,
            "logs": [
                {
                    "seq": turn.sequence,
                    "kind": (
                        "error"
                        if turn.state in {"failed", "abandoned"}
                        else "action"
                        if turn.state == "completed"
                        else "system"
                    ),
                    "state": turn.state,
                    "summary": _safe_public_turn_summary(turn),
                    "action": turn.action_type,
                    "error_code": turn.error_code,
                    "created_at": turn.created_at,
                }
                for turn in turns
            ],
            "log_cursor": turns[-1].sequence if turns else after_log_seq,
            "usage_today": {
                "actions": usage.actions if usage else 0,
                "calls": (
                    (usage.calls_reserved + usage.calls_charged) if usage else 0
                ),
                "input_tokens": usage.input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
                "total_tokens": (
                    (usage.tokens_reserved + usage.tokens_charged) if usage else 0
                ),
                "resets_at": datetime.combine(
                    _now().date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC
                ),
            },
        }
    )
    return data


async def _allocate_capacity_slot(
    db: AsyncSession, controller: HostedAgentController
) -> None:
    if controller.capacity_slot is not None:
        return
    for slot in range(settings.hosted_agent_runner_max_concurrent):
        try:
            async with db.begin_nested():
                result = await db.execute(
                    update(HostedAgentController)
                    .where(
                        HostedAgentController.id == controller.id,
                        HostedAgentController.capacity_slot.is_(None),
                    )
                    .values(capacity_slot=slot, updated_at=_now())
                )
                if result.rowcount != 1:
                    raise HostedAgentError(
                        "hosted_agent_capacity_conflict",
                        "Hosted Agent capacity changed concurrently",
                        409,
                    )
        except IntegrityError:
            continue
        await db.refresh(controller)
        return
    raise HostedAgentError(
        "hosted_agent_capacity_exhausted",
        "All Hosted Agent runtime slots are in use",
        409,
    )


async def set_desired_status(
    db: AsyncSession,
    *,
    controller_id: str,
    owner_user_id: str,
    desired_status: str,
) -> HostedAgentController:
    presence_user_id: str | None = None
    controller = await owner_controller(
        db, controller_id=controller_id, owner_user_id=owner_user_id, for_update=True
    )
    if controller.desired_status == "disabled":
        raise HostedAgentError("hosted_agent_disabled", "Hosted Agent is disabled", 409)
    if desired_status not in {"running", "paused"}:
        raise HostedAgentError("invalid_desired_status", "Invalid Hosted Agent status", 422)
    if desired_status == "running":
        await _allocate_capacity_slot(db, controller)
    if controller.desired_status != desired_status:
        controller.desired_status = desired_status
        controller.control_version += 1
        controller.next_tick_at = _now()
        if desired_status == "paused":
            pending_turns = (
                await db.execute(
                    select(HostedAgentTurn)
                    .where(
                        HostedAgentTurn.controller_id == controller.id,
                        HostedAgentTurn.state.in_(
                            {"observed", "budget_reserved", "calling", "decision_ready", "committing"}
                        ),
                    )
                    .with_for_update()
                )
            ).scalars().all()
            for turn in pending_turns:
                turn.state = "abandoned"
                turn.error_code = "paused_by_operator"
            controller.runtime_status = "idle"
            controller.lease_owner = None
            controller.lease_token = None
            controller.lease_expires_at = None
            controller.capacity_slot = None
        elif controller.runtime_status in {"error", "auth_blocked", "budget_paused"}:
            controller.runtime_status = (
                "provisioning" if controller.agent_player_id is None else "idle"
            )
            controller.last_error_code = None
            controller.retry_count = 0
            controller.provider_retry_at = None
    if desired_status == "paused" and controller.agent_player_id is not None:
        profile = (
            await db.execute(
                select(AgentPlayer)
                .where(AgentPlayer.id == controller.agent_player_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if profile is not None:
            profile.last_seen_at = None
            presence_user_id = profile.user_id
    await db.commit()
    await db.refresh(controller)
    if presence_user_id is not None:
        await _revoke_hosted_presence(presence_user_id)
    return controller


async def claim_due_controller(
    db: AsyncSession, *, worker_id: str
) -> HostedAgentController | None:
    now = _now()
    candidate_ids = (
        await db.execute(
            select(HostedAgentController.id)
            .where(
                HostedAgentController.desired_status == "running",
                HostedAgentController.capacity_slot.is_not(None),
                HostedAgentController.runtime_status.in_(HOSTED_LEASE_STATES),
                HostedAgentController.next_tick_at <= now,
                or_(
                    HostedAgentController.lease_expires_at.is_(None),
                    HostedAgentController.lease_expires_at <= now,
                ),
            )
            .order_by(HostedAgentController.next_tick_at, HostedAgentController.created_at)
            .limit(10)
        )
    ).scalars().all()
    for controller_id in candidate_ids:
        token = secrets.token_hex(24)
        expires = now + timedelta(seconds=settings.hosted_agent_runner_lease_seconds)
        claimed = await db.execute(
            update(HostedAgentController)
            .where(
                HostedAgentController.id == controller_id,
                HostedAgentController.desired_status == "running",
                HostedAgentController.capacity_slot.is_not(None),
                HostedAgentController.runtime_status.in_(HOSTED_LEASE_STATES),
                HostedAgentController.next_tick_at <= now,
                or_(
                    HostedAgentController.lease_expires_at.is_(None),
                    HostedAgentController.lease_expires_at <= now,
                ),
            )
            .values(
                runtime_status="claimed",
                lease_owner=worker_id,
                lease_token=token,
                lease_epoch=HostedAgentController.lease_epoch + 1,
                lease_expires_at=expires,
                heartbeat_at=now,
                updated_at=now,
            )
        )
        if claimed.rowcount == 1:
            await db.commit()
            return await db.get(HostedAgentController, controller_id)
        await db.rollback()
    return None


async def renew_controller_lease(
    db: AsyncSession, *, controller: HostedAgentController
) -> bool:
    now = _now()
    result = await db.execute(
        update(HostedAgentController)
        .where(
            HostedAgentController.id == controller.id,
            HostedAgentController.lease_owner == controller.lease_owner,
            HostedAgentController.lease_token == controller.lease_token,
            HostedAgentController.lease_epoch == controller.lease_epoch,
            HostedAgentController.desired_status == "running",
            HostedAgentController.control_version == controller.control_version,
            HostedAgentController.lease_expires_at > now,
        )
        .values(
            heartbeat_at=now,
            lease_expires_at=now
            + timedelta(seconds=settings.hosted_agent_runner_lease_seconds),
            updated_at=now,
        )
    )
    await db.commit()
    return result.rowcount == 1


async def reserve_daily_budget(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    reserve_tokens: int,
) -> HostedAgentDailyUsage:
    today = _now().date()
    usage = (
        await db.execute(
            select(HostedAgentDailyUsage)
            .where(
                HostedAgentDailyUsage.controller_id == controller.id,
                HostedAgentDailyUsage.usage_date == today,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if usage is None:
        usage = HostedAgentDailyUsage(controller_id=controller.id, usage_date=today)
        db.add(usage)
        await db.flush()
    if usage.calls_reserved + usage.calls_charged >= controller.max_provider_calls_per_day:
        raise HostedAgentError("provider_call_budget_exhausted", "Daily provider call limit reached", 429)
    if usage.tokens_reserved + usage.tokens_charged + reserve_tokens > controller.max_tokens_per_day:
        raise HostedAgentError("token_budget_exhausted", "Daily token limit reached", 429)
    usage.calls_reserved += 1
    usage.tokens_reserved += reserve_tokens
    usage.updated_at = _now()
    await db.commit()
    return usage


async def settle_daily_budget(
    db: AsyncSession,
    *,
    controller_id: str,
    usage_date: date,
    reserve_tokens: int,
    input_tokens: int,
    output_tokens: int,
) -> bool:
    controller = await db.get(HostedAgentController, controller_id)
    if controller is None:
        raise HostedAgentError("hosted_agent_not_found", "Hosted Agent not found", 404)
    usage = (
        await db.execute(
            select(HostedAgentDailyUsage)
            .where(
                HostedAgentDailyUsage.controller_id == controller_id,
                HostedAgentDailyUsage.usage_date == usage_date,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if usage is None:
        raise HostedAgentError("budget_reservation_lost", "Hosted Agent budget reservation was lost", 409)
    admitted_tokens = max(0, reserve_tokens)
    reported_input = max(0, input_tokens)
    reported_output = max(0, output_tokens)
    reported_total = reported_input + reported_output
    within_reservation = reported_total <= admitted_tokens
    # A malicious or incompatible provider must not make the hard counter
    # exceed the amount admitted before the call. An over-reservation report
    # tells the worker to stop processing the response and saturates the
    # remaining daily allowance below; no later call can exploit it.
    charged_input = min(reported_input, admitted_tokens)
    charged_output = min(
        reported_output, max(0, admitted_tokens - charged_input)
    )
    usage.calls_reserved = max(0, usage.calls_reserved - 1)
    usage.tokens_reserved = max(0, usage.tokens_reserved - admitted_tokens)
    usage.calls_charged += 1
    if within_reservation:
        usage.tokens_charged += charged_input + charged_output
    else:
        # Saturate the remaining daily allowance. Even a malicious provider
        # usage report cannot push the counter above the configured cap or
        # leave room for another call after breaking the reservation contract.
        usage.tokens_charged = max(
            usage.tokens_charged,
            controller.max_tokens_per_day - usage.tokens_reserved,
        )
    usage.input_tokens += charged_input
    usage.output_tokens += charged_output
    usage.updated_at = _now()
    await db.commit()
    return within_reservation


async def fail_unbilled_turn_and_release_controller(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    turn_id: str,
    error_code: str,
    runtime_status: str = "backoff",
    retry_seconds: int = 30,
) -> bool:
    """Atomically release a proven-unbilled decision and its controller lease."""
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        await db.rollback()
        return False
    turn = (
        await db.execute(
            select(HostedAgentTurn)
            .where(
                HostedAgentTurn.id == turn_id,
                HostedAgentTurn.controller_id == locked.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        turn is None
        or turn.state != "calling"
        or turn.budget_date is None
        or turn.reserved_tokens < 0
    ):
        await db.rollback()
        return False
    usage = (
        await db.execute(
            select(HostedAgentDailyUsage)
            .where(
                HostedAgentDailyUsage.controller_id == locked.id,
                HostedAgentDailyUsage.usage_date == turn.budget_date,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        usage is None
        or usage.calls_reserved < 1
        or usage.tokens_reserved < turn.reserved_tokens
    ):
        await db.rollback()
        return False

    now = _now()
    released_tokens = turn.reserved_tokens
    usage.calls_reserved -= 1
    usage.tokens_reserved -= released_tokens
    usage.updated_at = now
    turn.reserved_tokens = 0
    turn.state = "failed"
    turn.error_code = error_code[:100]
    locked.runtime_status = runtime_status
    locked.lease_owner = None
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.heartbeat_at = now
    if locked.agent_player_id is not None:
        locked.last_presence_at = now
    locked.last_error_code = error_code[:100]
    locked.retry_count += 1
    locked.provider_retry_at = now + timedelta(seconds=max(1, retry_seconds))
    locked.next_tick_at = now + timedelta(seconds=locked.heartbeat_seconds)
    locked.updated_at = now
    await db.commit()
    return True


def _apply_unknown_provider_outcome_block(
    controller: HostedAgentController, *, now: datetime
) -> None:
    """Fence a possibly-billed completion until an operator explicitly resumes."""
    controller.desired_status = "paused"
    controller.runtime_status = "error"
    controller.control_version += 1
    controller.capacity_slot = None
    controller.lease_owner = None
    controller.lease_token = None
    controller.lease_expires_at = None
    controller.heartbeat_at = now
    controller.last_error_code = "provider_outcome_unknown"
    controller.retry_count += 1
    controller.provider_retry_at = None
    controller.next_tick_at = now
    controller.updated_at = now


async def _clear_hosted_profile_presence(
    db: AsyncSession, controller: HostedAgentController
) -> str | None:
    if controller.agent_player_id is None:
        return None
    profile = (
        await db.execute(
            select(AgentPlayer)
            .where(AgentPlayer.id == controller.agent_player_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if profile is None:
        return None
    profile.last_seen_at = None
    return profile.user_id


async def _revoke_hosted_presence(user_id: str) -> None:
    try:
        from app.ws.manager import manager

        await manager.revoke_agent_presence(user_id)
    except Exception as exc:
        raise HostedAgentError(
            "presence_revoke_failed",
            "Hosted Agent was paused but its realtime presence could not be revoked",
            503,
        ) from exc


async def block_unknown_provider_outcome(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    turn_id: str | None = None,
) -> bool:
    """Persist an ambiguous provider outcome without releasing its reservation."""
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        await db.rollback()
        return False
    if turn_id is not None:
        turn = (
            await db.execute(
                select(HostedAgentTurn)
                .where(
                    HostedAgentTurn.id == turn_id,
                    HostedAgentTurn.controller_id == locked.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if turn is None or turn.state != "calling":
            await db.rollback()
            return False
        turn.state = "failed"
        turn.error_code = "provider_outcome_unknown"
    _apply_unknown_provider_outcome_block(locked, now=_now())
    presence_user_id = await _clear_hosted_profile_presence(db, locked)
    await db.commit()
    if presence_user_id is not None:
        await _revoke_hosted_presence(presence_user_id)
    return True


async def reserve_turn_provider_budget(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    turn_id: str,
    reserve_tokens: int,
) -> HostedAgentTurn:
    """Atomically transition an observed decision turn to billable calling."""
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        raise HostedAgentError(
            "controller_lease_lost", "Hosted Agent controller lease was lost", 409
        )
    turn = (
        await db.execute(
            select(HostedAgentTurn)
            .where(
                HostedAgentTurn.id == turn_id,
                HostedAgentTurn.controller_id == locked.id,
            )
            .with_for_update()
        )
    ).scalar_one()
    if turn.state != "observed":
        raise HostedAgentError("turn_state_conflict", "Hosted Agent turn changed", 409)
    today = _now().date()
    usage = (
        await db.execute(
            select(HostedAgentDailyUsage)
            .where(
                HostedAgentDailyUsage.controller_id == locked.id,
                HostedAgentDailyUsage.usage_date == today,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if usage is None:
        usage = HostedAgentDailyUsage(controller_id=locked.id, usage_date=today)
        db.add(usage)
        await db.flush()
    admitted_tokens = max(0, int(reserve_tokens))
    if usage.calls_reserved + usage.calls_charged >= locked.max_provider_calls_per_day:
        raise HostedAgentError(
            "provider_call_budget_exhausted", "Daily provider call limit reached", 429
        )
    if usage.tokens_reserved + usage.tokens_charged + admitted_tokens > locked.max_tokens_per_day:
        raise HostedAgentError(
            "token_budget_exhausted", "Daily token limit reached", 429
        )
    usage.calls_reserved += 1
    usage.tokens_reserved += admitted_tokens
    usage.updated_at = _now()
    turn.state = "calling"
    turn.budget_date = today
    turn.reserved_tokens = admitted_tokens
    await db.commit()
    return turn


async def begin_provider_stage_call(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    stage: str,
    reserve_tokens: int,
) -> HostedAgentTurn:
    """Durably reserve and mark a provisioning call before any network send."""
    if stage not in {"preflight", "identity"}:
        raise ValueError("unsupported hosted provider stage")
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        raise HostedAgentError(
            "controller_lease_lost", "Hosted Agent controller lease was lost", 409
        )
    calling_turns = (
        await db.execute(
            select(HostedAgentTurn)
            .where(
                HostedAgentTurn.controller_id == locked.id,
                HostedAgentTurn.state == "calling",
            )
            .order_by(HostedAgentTurn.sequence)
            .with_for_update()
        )
    ).scalars().all()
    current_ambiguous = next(
        (turn for turn in calling_turns if turn.control_version == locked.control_version),
        None,
    )
    if current_ambiguous is not None:
        for turn in calling_turns:
            if turn.control_version == locked.control_version:
                turn.state = "failed"
                turn.error_code = "provider_outcome_unknown"
        _apply_unknown_provider_outcome_block(locked, now=_now())
        presence_user_id = await _clear_hosted_profile_presence(db, locked)
        await db.commit()
        if presence_user_id is not None:
            await _revoke_hosted_presence(presence_user_id)
        raise HostedAgentError(
            "provider_outcome_unknown",
            "A provider request may have completed; explicit operator resume is required",
            409,
        )
    # A PATCH/start increments control_version and is the explicit operator
    # acknowledgement required to supersede an older ambiguous call.
    for turn in calling_turns:
        turn.state = "failed"
        turn.error_code = "superseded_by_operator_change"

    today = _now().date()
    usage = (
        await db.execute(
            select(HostedAgentDailyUsage)
            .where(
                HostedAgentDailyUsage.controller_id == locked.id,
                HostedAgentDailyUsage.usage_date == today,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if usage is None:
        usage = HostedAgentDailyUsage(controller_id=locked.id, usage_date=today)
        db.add(usage)
        await db.flush()
    admitted_tokens = max(0, int(reserve_tokens))
    if usage.calls_reserved + usage.calls_charged >= locked.max_provider_calls_per_day:
        raise HostedAgentError(
            "provider_call_budget_exhausted", "Daily provider call limit reached", 429
        )
    if usage.tokens_reserved + usage.tokens_charged + admitted_tokens > locked.max_tokens_per_day:
        raise HostedAgentError(
            "token_budget_exhausted", "Daily token limit reached", 429
        )
    usage.calls_reserved += 1
    usage.tokens_reserved += admitted_tokens
    usage.updated_at = _now()
    locked.turn_sequence += 1
    turn_id = str(uuid.uuid4())
    turn = HostedAgentTurn(
        id=turn_id,
        controller_id=locked.id,
        sequence=locked.turn_sequence,
        state="calling",
        lease_epoch=locked.lease_epoch,
        control_version=locked.control_version,
        observation_version=1,
        observation_envelope=encrypt_turn_value(
            turn_id=turn_id,
            field_name=HOSTED_OBSERVATION_FIELD,
            value={"kind": "provider_stage", "stage": stage},
        ),
        action_type=f"provider_{stage}",
        budget_date=today,
        reserved_tokens=admitted_tokens,
    )
    db.add(turn)
    await db.commit()
    return turn


async def complete_provider_stage_call(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    turn_id: str,
    result_value: dict[str, Any],
    usage: dict[str, Any],
    success: bool = True,
    error_code: str | None = None,
) -> bool | None:
    """Atomically meter and encrypt a successful provisioning-stage result."""
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        await db.rollback()
        return None
    turn = (
        await db.execute(
            select(HostedAgentTurn)
            .where(
                HostedAgentTurn.id == turn_id,
                HostedAgentTurn.controller_id == locked.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if turn is None or turn.state != "calling" or turn.budget_date is None:
        await db.rollback()
        return None
    daily = (
        await db.execute(
            select(HostedAgentDailyUsage)
            .where(
                HostedAgentDailyUsage.controller_id == locked.id,
                HostedAgentDailyUsage.usage_date == turn.budget_date,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        daily is None
        or daily.calls_reserved < 1
        or daily.tokens_reserved < turn.reserved_tokens
    ):
        await db.rollback()
        return None
    reported_input = max(0, int(usage.get("input_tokens", 0)))
    reported_output = max(0, int(usage.get("output_tokens", 0)))
    reported_total = reported_input + reported_output
    admitted_tokens = turn.reserved_tokens
    within_reservation = reported_total <= admitted_tokens
    charged_input = min(reported_input, admitted_tokens)
    charged_output = min(reported_output, max(0, admitted_tokens - charged_input))
    daily.calls_reserved -= 1
    daily.tokens_reserved -= admitted_tokens
    daily.calls_charged += 1
    if within_reservation:
        daily.tokens_charged += charged_input + charged_output
    else:
        daily.tokens_charged = max(
            daily.tokens_charged,
            locked.max_tokens_per_day - daily.tokens_reserved,
        )
    daily.input_tokens += charged_input
    daily.output_tokens += charged_output
    now = _now()
    daily.updated_at = now
    turn.result_version = 1
    turn.result_envelope = encrypt_turn_value(
        turn_id=turn.id,
        field_name=HOSTED_RESULT_FIELD,
        value=result_value,
    )
    turn.provider_request_id = usage.get("provider_request_id")
    turn.provider_calls = 1
    turn.input_tokens = charged_input
    turn.output_tokens = charged_output
    turn.total_tokens = charged_input + charged_output
    turn.state = "completed" if within_reservation and success else "failed"
    turn.error_code = (
        None
        if within_reservation and success
        else (error_code or "provider_usage_exceeded_reservation")[:100]
    )
    if turn.state == "completed":
        turn.public_summary = (
            "完成 Provider 身份初始化"
            if turn.action_type == "provider_identity"
            else "完成 Provider 兼容性检查"
        )
    else:
        turn.public_summary = "Provider 阶段未能完成"
    # Provider stages are operational records, not town-memory turns.
    turn.journaled_at = now
    await db.commit()
    return within_reservation


async def completed_provider_stage_result(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    stage: str,
) -> dict[str, Any] | None:
    turn = (
        await db.execute(
            select(HostedAgentTurn)
            .where(
                HostedAgentTurn.controller_id == controller.id,
                HostedAgentTurn.control_version == controller.control_version,
                HostedAgentTurn.action_type == f"provider_{stage}",
                HostedAgentTurn.state == "completed",
            )
            .order_by(HostedAgentTurn.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if turn is None or not turn.result_envelope:
        return None
    return decrypt_turn_value(turn, HOSTED_RESULT_FIELD)


async def create_hosted_identity(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    display_name: str,
    sprite_key: str,
    public_role: dict[str, Any],
    public_identity: dict[str, Any],
    play_secret_bundle: dict[str, Any],
) -> AgentPlayer:
    """Activate Agent identity and persist its encrypted play token atomically."""
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if (
        locked.desired_status != "running"
        or locked.runtime_status != "claimed"
        or locked.lease_token != controller.lease_token
        or locked.lease_epoch != controller.lease_epoch
        or locked.control_version != controller.control_version
        or (_aware(locked.lease_expires_at) or _now()) <= _now()
    ):
        raise HostedAgentError("controller_lease_lost", "Hosted Agent controller lease was lost", 409)
    if locked.agent_player_id:
        profile = await db.get(AgentPlayer, locked.agent_player_id)
        if profile is None:
            raise HostedAgentError("hosted_identity_incomplete", "Hosted Agent identity is incomplete", 409)
        return profile

    profile, user, resident, pair = await register_agent_player(
        db,
        name=display_name,
        sprite_key=sprite_key,
        model_label=locked.model,
        client={"name": "simverse-hosted-agent", "version": "1"},
        role=public_role,
        public_visible=True,
        slug_override=_hosted_identity_slug(locked.id),
        commit=False,
    )
    profile, play, view = await redeem_pairing(
        db, pair.plaintext, profile.id, commit=False
    )
    profile.control_kind = "hosted_agent"
    profile.client_json = {"name": "simverse-hosted-agent", "version": "1"}
    user.settings_json = {**(user.settings_json or {}), "principal_kind": "hosted_agent"}
    resident.meta_json = {
        **(resident.meta_json or {}),
        "origin": "hosted_agent",
        "agent_controlled": True,
    }
    # Admin-hosted identities have no spectator secret. The public watcher
    # projection is owner-authenticated and the one-time plaintext is discarded.
    view.credential.revoked_at = _now()
    pair.credential.revoked_at = _now()
    play_secret_bundle = {**play_secret_bundle, "play_token": play.plaintext}
    locked.secret_version += 1
    locked.secret_envelope = encrypt_secret_bundle(locked.id, play_secret_bundle)
    locked.agent_player_id = profile.id
    locked.identity_json = {
        **public_identity,
        "display_name": resident.name,
        "slug": resident.slug,
        "role_card": public_role,
    }
    locked.runtime_status = "idle"
    locked.provider_validation_required = False
    locked.last_error_code = None
    locked.retry_count = 0
    locked.next_tick_at = _now()
    locked.lease_owner = None
    locked.lease_token = None
    locked.lease_expires_at = None
    await db.commit()
    return profile


def _lease_matches(
    controller: HostedAgentController, *, lease_token: str, lease_epoch: int, control_version: int
) -> bool:
    expires = _aware(controller.lease_expires_at)
    return (
        controller.desired_status == "running"
        and controller.runtime_status == "claimed"
        and controller.lease_token == lease_token
        and controller.lease_epoch == lease_epoch
        and controller.control_version == control_version
        and expires is not None
        and expires > _now()
    )


async def release_controller(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    runtime_status: str = "idle",
    next_tick_at: datetime | None = None,
    last_presence_at: datetime | None = None,
) -> bool:
    now = _now()
    result = await db.execute(
        update(HostedAgentController)
        .where(
            HostedAgentController.id == controller.id,
            HostedAgentController.desired_status == "running",
            HostedAgentController.runtime_status == "claimed",
            HostedAgentController.lease_token == controller.lease_token,
            HostedAgentController.lease_epoch == controller.lease_epoch,
            HostedAgentController.control_version == controller.control_version,
        )
        .values(
            runtime_status=runtime_status,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=now,
            last_presence_at=last_presence_at,
            next_tick_at=next_tick_at
            or now + timedelta(seconds=controller.heartbeat_seconds),
            updated_at=now,
        )
    )
    await db.commit()
    return result.rowcount == 1


async def fail_controller(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    error_code: str,
    runtime_status: str = "error",
    retry_seconds: int = 30,
    presence_ok: bool = False,
) -> bool:
    now = _now()
    retry_at = now + timedelta(seconds=max(1, retry_seconds))
    next_presence = now + timedelta(seconds=controller.heartbeat_seconds)
    result = await db.execute(
        update(HostedAgentController)
        .where(
            HostedAgentController.id == controller.id,
            HostedAgentController.desired_status == "running",
            HostedAgentController.runtime_status == "claimed",
            HostedAgentController.lease_token == controller.lease_token,
            HostedAgentController.lease_epoch == controller.lease_epoch,
            HostedAgentController.control_version == controller.control_version,
        )
        .values(
            runtime_status=runtime_status,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=now,
            last_presence_at=now if presence_ok else controller.last_presence_at,
            last_error_code=error_code,
            retry_count=HostedAgentController.retry_count + 1,
            provider_retry_at=retry_at,
            next_tick_at=next_presence,
            updated_at=now,
        )
    )
    await db.commit()
    return result.rowcount == 1


async def complete_provisioning_without_identity_change(
    db: AsyncSession, *, controller: HostedAgentController
) -> bool:
    now = _now()
    result = await db.execute(
        update(HostedAgentController)
        .where(
            HostedAgentController.id == controller.id,
            HostedAgentController.desired_status == "running",
            HostedAgentController.runtime_status == "claimed",
            HostedAgentController.lease_token == controller.lease_token,
            HostedAgentController.lease_epoch == controller.lease_epoch,
            HostedAgentController.control_version == controller.control_version,
            HostedAgentController.agent_player_id.is_not(None),
        )
        .values(
            runtime_status="idle",
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=now,
            last_error_code=None,
            retry_count=0,
            provider_retry_at=None,
            provider_validation_required=False,
            next_tick_at=now,
            updated_at=now,
        )
    )
    await db.commit()
    return result.rowcount == 1


async def create_turn_journal(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    observation: dict[str, Any],
) -> HostedAgentTurn:
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        raise HostedAgentError("controller_lease_lost", "Hosted Agent controller lease was lost", 409)
    locked.turn_sequence += 1
    turn_id = str(uuid.uuid4())
    turn = HostedAgentTurn(
        id=turn_id,
        controller_id=locked.id,
        sequence=locked.turn_sequence,
        state="observed",
        lease_epoch=locked.lease_epoch,
        control_version=locked.control_version,
        observation_seq=int(observation.get("observation_seq", 0)),
        event_cursor=int(observation.get("event_cursor", 0)),
        observation_version=1,
        observation_envelope=encrypt_turn_value(
            turn_id=turn_id, field_name=HOSTED_OBSERVATION_FIELD, value=observation
        ),
    )
    db.add(turn)
    await db.commit()
    return turn


async def adopt_recoverable_turn(
    db: AsyncSession, *, controller: HostedAgentController
) -> HostedAgentTurn | None:
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        raise HostedAgentError("controller_lease_lost", "Hosted Agent controller lease was lost", 409)
    turns = (
        await db.execute(
            select(HostedAgentTurn)
            .where(
                HostedAgentTurn.controller_id == locked.id,
                HostedAgentTurn.state.in_(
                    {"observed", "budget_reserved", "calling", "decision_ready", "committing"}
                ),
            )
            .order_by(HostedAgentTurn.sequence.desc())
            .with_for_update()
        )
    ).scalars().all()
    ambiguous = next((turn for turn in turns if turn.state == "calling"), None)
    if ambiguous is not None:
        for turn in turns:
            if turn.state == "calling":
                turn.state = "failed"
                turn.error_code = "provider_outcome_unknown"
            elif turn.state not in {"completed", "failed", "abandoned"}:
                turn.state = "abandoned"
                turn.error_code = "blocked_by_unknown_provider_outcome"
        _apply_unknown_provider_outcome_block(locked, now=_now())
        presence_user_id = await _clear_hosted_profile_presence(db, locked)
        await db.commit()
        if presence_user_id is not None:
            await _revoke_hosted_presence(presence_user_id)
        raise HostedAgentError(
            "provider_outcome_unknown",
            "A provider request may have completed; explicit operator resume is required",
            409,
        )
    recoverable: HostedAgentTurn | None = None
    for turn in turns:
        if recoverable is None and turn.state in {"decision_ready", "committing"}:
            turn.state = "decision_ready"
            turn.lease_epoch = locked.lease_epoch
            turn.control_version = locked.control_version
            recoverable = turn
        elif turn.state not in {"completed", "failed", "abandoned"}:
            turn.state = "abandoned"
            turn.error_code = "worker_restarted_before_decision"
    await db.commit()
    return recoverable


async def persist_turn_decision(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    turn_id: str,
    decision: dict[str, Any],
    action_type: str,
    public_summary: str,
    usage: dict[str, Any],
) -> HostedAgentTurn:
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        raise HostedAgentError("controller_lease_lost", "Hosted Agent controller lease was lost", 409)
    turn = (
        await db.execute(
            select(HostedAgentTurn).where(HostedAgentTurn.id == turn_id).with_for_update()
        )
    ).scalar_one()
    if turn.state != "calling":
        raise HostedAgentError("turn_state_conflict", "Hosted Agent turn changed", 409)
    action_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"simverse-hosted:{controller.id}:{turn.sequence}"))
    turn.decision_version = 1
    turn.decision_envelope = encrypt_turn_value(
        turn_id=turn.id, field_name=HOSTED_DECISION_FIELD, value=decision
    )
    turn.action_id = action_id
    turn.action_type = action_type
    turn.public_summary = public_summary[:280]
    turn.provider_request_id = usage.get("provider_request_id")
    turn.provider_calls = 1
    turn.input_tokens = max(0, int(usage.get("input_tokens", 0)))
    turn.output_tokens = max(0, int(usage.get("output_tokens", 0)))
    turn.total_tokens = max(0, int(usage.get("total_tokens", 0)))
    turn.state = "decision_ready"
    await db.commit()
    return turn


async def mark_turn_committing(
    db: AsyncSession, *, controller: HostedAgentController, turn_id: str
) -> HostedAgentTurn:
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        raise HostedAgentError("controller_lease_lost", "Hosted Agent controller lease was lost", 409)
    turn = (
        await db.execute(
            select(HostedAgentTurn).where(HostedAgentTurn.id == turn_id).with_for_update()
        )
    ).scalar_one()
    if turn.state not in {"decision_ready", "committing"}:
        raise HostedAgentError("turn_state_conflict", "Hosted Agent turn changed", 409)
    turn.state = "committing"
    await db.commit()
    return turn


async def mark_turn_failed(
    db: AsyncSession, *, turn_id: str, error_code: str
) -> None:
    turn = await db.get(HostedAgentTurn, turn_id)
    if turn is not None and turn.state not in {"completed", "failed", "abandoned"}:
        turn.state = "failed"
        turn.error_code = error_code
        await db.commit()


async def abandon_turn_and_release_controller(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    turn_id: str,
    error_code: str,
) -> bool:
    """Finish a deterministic 4xx without replaying a now-invalid decision."""
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        await db.rollback()
        return False
    turn = (
        await db.execute(
            select(HostedAgentTurn)
            .where(
                HostedAgentTurn.id == turn_id,
                HostedAgentTurn.controller_id == locked.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if turn is None or turn.state == "completed":
        await db.rollback()
        return False
    now = _now()
    turn.state = "failed"
    turn.error_code = error_code[:100]
    locked.runtime_status = "idle"
    locked.lease_owner = None
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.heartbeat_at = now
    locked.last_presence_at = now
    locked.next_action_at = now
    locked.next_tick_at = now
    locked.last_error_code = error_code[:100]
    await db.commit()
    return True


async def reconcile_private_journal(
    db: AsyncSession, *, controller: HostedAgentController
) -> dict[str, Any]:
    """Fold completed encrypted turns into a bounded encrypted private journal."""
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        raise HostedAgentError("controller_lease_lost", "Hosted Agent controller lease was lost", 409)
    bundle = decrypt_secret_bundle(locked)
    journal = bundle.get("journal")
    if not isinstance(journal, list):
        journal = []
    disclosed = bundle.get("disclosed_player_slugs")
    if not isinstance(disclosed, list):
        disclosed = []
    disclosed_set = {str(item) for item in disclosed if isinstance(item, str)}
    turns = (
        await db.execute(
            select(HostedAgentTurn)
            .where(
                HostedAgentTurn.controller_id == locked.id,
                HostedAgentTurn.state == "completed",
                HostedAgentTurn.journaled_at.is_(None),
            )
            .order_by(HostedAgentTurn.sequence)
            .limit(100)
            .with_for_update()
        )
    ).scalars().all()
    now = _now()
    for turn in turns:
        observation = decrypt_turn_value(turn, HOSTED_OBSERVATION_FIELD)
        decision = decrypt_turn_value(turn, HOSTED_DECISION_FIELD)
        result = (
            decrypt_turn_value(turn, HOSTED_RESULT_FIELD)
            if turn.result_envelope
            else {}
        )
        events = observation.get("recent_events")
        journal.append(
            {
                "turn": turn.sequence,
                "action": turn.action_type,
                "summary": _safe_public_turn_summary(turn),
                "events": events[:20] if isinstance(events, list) else [],
                "result": result,
                "at": turn.updated_at.isoformat() if turn.updated_at else now.isoformat(),
            }
        )
        if turn.action_type == "message_player":
            player_slug = decision.get("player_slug")
            if isinstance(player_slug, str) and player_slug:
                disclosed_set.add(player_slug)
        turn.journaled_at = now
    if turns:
        bundle["journal"] = journal[-20:]
        bundle["disclosed_player_slugs"] = sorted(disclosed_set)[-500:]
        locked.secret_version += 1
        locked.secret_envelope = encrypt_secret_bundle(locked.id, bundle)
        await db.commit()
    return bundle


async def replace_secret_bundle_under_lease(
    db: AsyncSession,
    *,
    controller: HostedAgentController,
    bundle: dict[str, Any],
) -> None:
    locked = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.id == controller.id)
            .with_for_update()
        )
    ).scalar_one()
    if not _lease_matches(
        locked,
        lease_token=str(controller.lease_token),
        lease_epoch=controller.lease_epoch,
        control_version=controller.control_version,
    ):
        raise HostedAgentError("controller_lease_lost", "Hosted Agent controller lease was lost", 409)
    locked.secret_version += 1
    locked.secret_envelope = encrypt_secret_bundle(locked.id, bundle)
    await db.commit()
