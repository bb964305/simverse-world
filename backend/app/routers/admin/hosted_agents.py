"""Admin-owned durable hosted Agent controllers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.agent_player import AgentPlayer
from app.models.hosted_agent import HostedAgentController
from app.models.resident import Resident
from app.models.user import User
from app.models.web3_agent_passport import Web3AgentPassport
from app.rate_limit import limiter
from app.services.auth_service import get_current_user
from app.services.hosted_agent_provider import (
    HostedProviderError,
    hosted_identity_token_reservation,
    hosted_preflight_token_reservation,
    validate_hosted_display_name,
    validate_hosted_identity_text,
    validate_hosted_provider_base_url,
)
from app.services.hosted_agent_service import (
    HostedAgentError,
    controller_public,
    controller_state,
    create_controller,
    create_request_hash,
    decrypt_secret_bundle,
    encrypt_secret_bundle,
    owner_controller,
    set_desired_status,
)
from app.services.resident_placement import SPRITE_KEYS


router = APIRouter(prefix="/hosted-agents", tags=["admin-hosted-agents"])
MAX_HOSTED_AGENT_CREATE_BODY_BYTES = 16 * 1024


async def require_hosted_agent_owner(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Allow admins and wallet users with a confirmed onchain Passport.

    Every controller query remains scoped to ``owner_user_id`` below; this
    dependency only opens the existing self-service runtime to its owner.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if user.is_banned:
        raise HTTPException(status_code=403, detail="Account is banned")
    if user.is_admin:
        return user
    if not user.wallet_address:
        raise HTTPException(status_code=403, detail="Wallet identity required")
    passport_id = (
        await db.execute(
            select(Web3AgentPassport.id)
            .where(Web3AgentPassport.user_id == user.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if passport_id is None:
        raise HTTPException(status_code=403, detail="Confirmed Agent Passport required")
    return user


class HostedAgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    base_url: str
    api_key: SecretStr
    model: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    sprite_key: str = Field(default="埃迪", min_length=1, max_length=100)
    goal: str = Field(
        default="认识小镇的居民，并逐渐建立稳定而有益的日常生活",
        min_length=1,
        max_length=400,
    )
    heartbeat_seconds: int = Field(default=30, ge=15, le=60)
    action_interval_seconds: int = Field(default=30, ge=5, le=3600)
    daily_action_limit: int = Field(default=200, ge=1, le=1000)
    daily_token_limit: int = Field(default=200_000, ge=1000, le=10_000_000)
    max_output_tokens: int = Field(default=600, ge=1, le=2000)


class HostedAgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    base_url: str | None = None
    api_key: SecretStr | None = None
    model: str | None = Field(default=None, min_length=1, max_length=100)
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    goal: str | None = Field(default=None, min_length=1, max_length=400)
    heartbeat_seconds: int | None = Field(default=None, ge=15, le=60)
    action_interval_seconds: int | None = Field(default=None, ge=5, le=3600)
    daily_action_limit: int | None = Field(default=None, ge=1, le=1000)
    daily_token_limit: int | None = Field(default=None, ge=1000, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=2000)


async def _parse_create_body(request: Request) -> HostedAgentCreate:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError
        body = HostedAgentCreate.model_validate(payload)
        secret = body.api_key.get_secret_value()
        if len(secret) < 8 or len(secret) > 2000 or secret != secret.strip():
            raise ValueError
        # A reflected key in any public input would defeat write-only handling.
        if any(
            secret in value
            for value in (
                str(body.request_id),
                body.base_url,
                body.model,
                body.display_name,
                body.sprite_key,
                body.goal,
            )
        ):
            raise ValueError
        return body
    except (ValueError, TypeError, ValidationError):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_hosted_agent_request",
                "message": "Invalid Hosted Agent request",
            },
        )


async def _parse_patch_body(request: Request) -> HostedAgentPatch:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError
        body = HostedAgentPatch.model_validate(payload)
        if body.api_key is not None:
            secret = body.api_key.get_secret_value()
            if secret and (
                len(secret) < 8 or len(secret) > 2000 or secret != secret.strip()
            ):
                raise ValueError
            if secret and any(
                secret in value
                for value in (
                    body.base_url or "",
                    body.model or "",
                    body.display_name or "",
                    body.goal or "",
                )
            ):
                raise ValueError
        return body
    except (ValueError, TypeError, ValidationError):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_hosted_agent_request",
                "message": "Invalid Hosted Agent request",
            },
        )


def _fail(exc: HostedAgentError | HostedProviderError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.detail},
    )


def _require_enabled() -> None:
    if not settings.hosted_agent_runner_enabled:
        raise HTTPException(status_code=404, detail="Hosted Agent runner is disabled")


def _required_provisioning_tokens(*, display_name: str, public_goal: str) -> int:
    return hosted_preflight_token_reservation() + hosted_identity_token_reservation(
        display_name=display_name,
        public_goal=public_goal,
    )


def _public_text_values(value: Any):
    """Yield strings from server-owned fields that can reach public projections."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _public_text_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _public_text_values(item)


def _secret_reflected_in_public(secret: str, *values: Any) -> bool:
    return len(secret) >= 8 and any(
        secret in text
        for value in values
        for text in _public_text_values(value)
    )


@router.post("", status_code=202)
@limiter.limit("5/minute")
async def create_hosted_agent(
    request: Request,
    admin: User = Depends(require_hosted_agent_owner),
    body: HostedAgentCreate = Depends(_parse_create_body),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    try:
        display_name = validate_hosted_display_name(body.display_name)
        public_goal = validate_hosted_identity_text(
            body.goal, label="public_goal", max_chars=400
        )
        base_url, provider_host = await validate_hosted_provider_base_url(
            body.base_url.strip()
        )
    except (ValueError, HostedProviderError) as exc:
        if isinstance(exc, HostedProviderError):
            _fail(exc)
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_identity", "message": "Invalid resident identity input"},
        )
    model = body.model.strip()
    if not model:
        raise HTTPException(status_code=422, detail={"code": "invalid_model"})
    if body.sprite_key not in SPRITE_KEYS:
        raise HTTPException(status_code=422, detail={"code": "invalid_sprite_key"})
    if body.daily_token_limit < _required_provisioning_tokens(
        display_name=display_name,
        public_goal=public_goal,
    ):
        raise HTTPException(
            status_code=422,
            detail={"code": "daily_token_limit_too_low_for_provisioning"},
        )
    bundle = {
        "version": 1,
        "base_url": base_url,
        "api_key": body.api_key.get_secret_value(),
        "display_name": display_name,
        "sprite_key": body.sprite_key,
        "public_goal": public_goal,
    }
    policy = {
        "heartbeat_seconds": body.heartbeat_seconds,
        "action_interval_seconds": body.action_interval_seconds,
        "daily_action_limit": body.daily_action_limit,
        "daily_token_limit": body.daily_token_limit,
        "max_output_tokens": body.max_output_tokens,
    }
    digest = create_request_hash(
        {
            **bundle,
            "model": model,
            "policy": policy,
        }
    )
    try:
        controller, _created = await create_controller(
            db,
            owner_user_id=admin.id,
            request_id=str(body.request_id),
            request_hash=digest,
            provider_host=provider_host,
            model=model,
            bundle=bundle,
            policy=policy,
        )
    except HostedAgentError as exc:
        _fail(exc)
    return controller_public(controller)


@router.get("")
async def list_hosted_agents(
    request: Request,
    admin: User = Depends(require_hosted_agent_owner),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    items = (
        await db.execute(
            select(HostedAgentController)
            .where(HostedAgentController.owner_user_id == admin.id)
            .order_by(HostedAgentController.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    return {"items": [controller_public(item) for item in items], "total": len(items)}


@router.get("/{controller_id}/state")
async def get_hosted_agent_state(
    controller_id: str,
    request: Request,
    after_log_seq: int = Query(default=0, ge=0),
    admin: User = Depends(require_hosted_agent_owner),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    try:
        controller = await owner_controller(
            db, controller_id=controller_id, owner_user_id=admin.id
        )
        return await controller_state(
            db, controller=controller, after_log_seq=after_log_seq
        )
    except HostedAgentError as exc:
        _fail(exc)


@router.get("/{controller_id}")
async def get_hosted_agent(
    controller_id: str,
    request: Request,
    admin: User = Depends(require_hosted_agent_owner),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    try:
        controller = await owner_controller(
            db, controller_id=controller_id, owner_user_id=admin.id
        )
    except HostedAgentError as exc:
        _fail(exc)
    return controller_public(controller)


@router.patch("/{controller_id}")
async def patch_hosted_agent(
    controller_id: str,
    request: Request,
    admin: User = Depends(require_hosted_agent_owner),
    body: HostedAgentPatch = Depends(_parse_patch_body),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    try:
        controller = await owner_controller(
            db,
            controller_id=controller_id,
            owner_user_id=admin.id,
            for_update=True,
        )
        if controller.control_version != body.version:
            raise HostedAgentError(
                "version_conflict", "Hosted Agent was changed by another request", 409
            )
        bundle = decrypt_secret_bundle(controller)
        existing_secret = bundle.get("api_key")
        if isinstance(existing_secret, str) and len(existing_secret) >= 8:
            submitted_public_values = (
                body.base_url,
                body.model,
                body.display_name,
                body.goal,
            )
            if any(
                isinstance(value, str) and existing_secret in value
                for value in submitted_public_values
            ):
                raise HostedAgentError(
                    "invalid_hosted_agent_request",
                    "Invalid Hosted Agent request",
                    422,
                )
        replacement_secret = (
            body.api_key.get_secret_value()
            if body.api_key is not None and body.api_key.get_secret_value()
            else existing_secret
        )
        if isinstance(replacement_secret, str) and _secret_reflected_in_public(
            replacement_secret,
            controller.id,
            controller.request_id,
            controller.provider_host,
            controller.model,
            controller.identity_json,
            body.base_url,
            body.model,
            body.display_name,
            body.goal,
        ):
            raise HostedAgentError(
                "invalid_hosted_agent_request",
                "Invalid Hosted Agent request",
                422,
            )
        provider_changed = False
        if body.display_name is not None:
            if controller.agent_player_id is not None:
                raise HostedAgentError(
                    "identity_is_stable", "An active resident cannot be renamed", 409
                )
            bundle["display_name"] = validate_hosted_display_name(body.display_name)
        if body.goal is not None:
            goal = validate_hosted_identity_text(
                body.goal, label="public_goal", max_chars=400
            )
            bundle["public_goal"] = goal
            identity = dict(controller.identity_json or {})
            goals = dict(identity.get("goals") or {})
            goals["public"] = goal
            identity["goals"] = goals
            public_role = dict(identity.get("role_card") or {})
            public_role["goals"] = {
                **(public_role.get("goals") or {}),
                "public": goal,
            }
            public_role["soul_md"] = (
                f"{str(public_role.get('soul_md') or '').split('Present public goal:')[0].rstrip()} "
                f"Present public goal: {goal}"
            ).strip()[:2000]
            identity["role_card"] = public_role
            controller.identity_json = identity
            if controller.agent_player_id:
                profile = await db.get(AgentPlayer, controller.agent_player_id)
                if profile is not None:
                    profile.role_json = public_role
                    resident = await db.get(Resident, profile.resident_id)
                    if resident is not None:
                        resident.soul_md = public_role["soul_md"]
                        from app.services.content_guard import assert_resident_content_clean

                        assert_resident_content_clean(
                            name=resident.name,
                            ability_md=resident.ability_md,
                            persona_md=resident.persona_md,
                            soul_md=resident.soul_md,
                        )
        if body.base_url is not None:
            base_url, provider_host = await validate_hosted_provider_base_url(
                body.base_url.strip()
            )
            bundle["base_url"] = base_url
            controller.provider_host = provider_host
            provider_changed = True
        if body.api_key is not None and body.api_key.get_secret_value():
            bundle["api_key"] = body.api_key.get_secret_value()
            provider_changed = True
        if body.model is not None:
            model = body.model.strip()
            if not model:
                raise HostedAgentError("invalid_model", "Model is required", 422)
            controller.model = model
            identity = dict(controller.identity_json or {})
            identity["model_label"] = model
            controller.identity_json = identity
            if controller.agent_player_id:
                profile = await db.get(AgentPlayer, controller.agent_player_id)
                if profile is not None:
                    profile.model_label = model
            provider_changed = True
        final_token_limit = (
            body.daily_token_limit
            if body.daily_token_limit is not None
            else controller.max_tokens_per_day
        )
        if controller.agent_player_id is None and final_token_limit < _required_provisioning_tokens(
            display_name=str(bundle.get("display_name") or ""),
            public_goal=str(bundle.get("public_goal") or ""),
        ):
            raise HostedAgentError(
                "daily_token_limit_too_low_for_provisioning",
                "Daily token limit is too low for identity provisioning",
                422,
            )
        for attr, value in (
            ("heartbeat_seconds", body.heartbeat_seconds),
            ("action_interval_seconds", body.action_interval_seconds),
            ("max_actions_per_day", body.daily_action_limit),
            ("max_tokens_per_day", body.daily_token_limit),
            ("max_output_tokens", body.max_output_tokens),
        ):
            if value is not None:
                setattr(controller, attr, value)
        controller.secret_version += 1
        controller.secret_envelope = encrypt_secret_bundle(controller.id, bundle)
        controller.control_version += 1
        controller.lease_owner = None
        controller.lease_token = None
        controller.lease_expires_at = None
        controller.next_tick_at = datetime.now(UTC)
        if provider_changed:
            controller.runtime_status = "provisioning"
            controller.provider_validation_required = True
            controller.last_error_code = None
        await db.commit()
        await db.refresh(controller)
        return controller_public(controller)
    except (HostedAgentError, HostedProviderError) as exc:
        await db.rollback()
        _fail(exc)
    except ValueError:
        await db.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_identity", "message": "Invalid resident identity input"},
        )


@router.post("/{controller_id}/start")
async def start_hosted_agent(
    controller_id: str,
    request: Request,
    admin: User = Depends(require_hosted_agent_owner),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    try:
        return controller_public(
            await set_desired_status(
                db,
                controller_id=controller_id,
                owner_user_id=admin.id,
                desired_status="running",
            )
        )
    except HostedAgentError as exc:
        _fail(exc)


@router.post("/{controller_id}/stop")
async def stop_hosted_agent(
    controller_id: str,
    request: Request,
    admin: User = Depends(require_hosted_agent_owner),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    try:
        return controller_public(
            await set_desired_status(
                db,
                controller_id=controller_id,
                owner_user_id=admin.id,
                desired_status="paused",
            )
        )
    except HostedAgentError as exc:
        _fail(exc)
