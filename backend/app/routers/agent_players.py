"""Scoped REST surface for headless Agent players and read-only spectators."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.agent_player import (
    AgentActionReceipt,
    AgentNpcChatTurnReceipt,
    AgentPlayer,
)
from app.models.conversation import Conversation, Message
from app.models.resident import Resident
from app.models.user import User
from app.rate_limit import limiter
from app.services.agent_player_service import (
    AgentPlayerError,
    acknowledge_private_events,
    agent_me,
    create_agent_session_token,
    create_viewer_session_token,
    observation,
    perform_action,
    public_town_snapshot,
    redeem_pairing,
    register_agent_player,
    require_agent_session,
    require_viewer_session,
    resolve_opaque_credential,
    viewer_snapshot,
)
from app.services.coin_service import charge_pending, get_balance
from app.services.daily_reward_service import claim_daily_reward
from app.llm.budget import user_over_budget
from app.services.player_npc_chat_service import (
    build_single_turn_prompt,
    extract_player_chat_memories,
    generate_single_turn_reply,
    npc_chat_lock_owner,
    recover_expired_npc_chat_turns,
    release_npc_chat_lock_and_notify,
)
from app.ws.manager import manager


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["agent-players"])
MAX_ROLE_CARD_BYTES = 32 * 1024
MAX_ACTION_REQUEST_BYTES = 8 * 1024
MAX_NPC_CHAT_TEXT_CHARS = 1000
MAX_NPC_CHAT_CONTEXT_CHARS = 1000
MAX_NPC_CHAT_REQUEST_BYTES = 8 * 1024
NPC_CHAT_DISTANCE_TILES = 2
NPC_CHAT_CALL_TIMEOUT_SECONDS = max(1, int(settings.user_llm_timeout))
NPC_CHAT_LEASE_SECONDS = max(30, NPC_CHAT_CALL_TIMEOUT_SECONDS + 30)


def _fail(exc: AgentPlayerError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return auth.removeprefix("Bearer ").strip()


async def _agent_profile(request: Request, db: AsyncSession):
    try:
        profile = await require_agent_session(db, _bearer(request))
        await recover_expired_npc_chat_turns(db)
        profile.last_seen_at = datetime.now(UTC)
        await db.commit()
        await _refresh_agent_presence(db, profile)
        return profile
    except AgentPlayerError as exc:
        _fail(exc)


async def _refresh_agent_presence(db: AsyncSession, profile) -> None:
    """Best-effort expiring realtime projection for a REST-controlled avatar."""
    user = await db.get(User, profile.user_id)
    if user is None:
        return
    try:
        became_online = await manager.update_agent_position(
            user.id,
            float(user.last_x),
            float(user.last_y),
            "down",
            user.name,
            ttl_seconds=settings.agent_presence_ttl_seconds,
        )
        if became_online:
            await manager.broadcast(
                {
                    "type": "player_joined",
                    "player_id": user.id,
                    "name": user.name,
                    "x": user.last_x,
                    "y": user.last_y,
                    "direction": "down",
                    "agent_controlled": True,
                    "presence_ttl_seconds": settings.agent_presence_ttl_seconds,
                },
                exclude=user.id,
            )
    except Exception:
        logger.warning("Agent presence heartbeat failed", exc_info=True)


class ClientInfo(BaseModel):
    name: str = Field(default="play-simverse-as-player", max_length=100)
    version: str = Field(default="0.1.0", max_length=30)


class AgentApplicationRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    sprite_key: str = "埃迪"
    model_label: str | None = Field(default=None, max_length=100)
    role_card: dict[str, Any] = Field(default_factory=dict)
    client: ClientInfo = Field(default_factory=ClientInfo)
    public_visible: bool = True


class PairingRedeemRequest(BaseModel):
    application_id: str | None = None
    pairing_code: str = Field(min_length=1, max_length=512)


class AgentSessionRequest(BaseModel):
    client: ClientInfo = Field(default_factory=ClientInfo)


class AgentActionRequest(BaseModel):
    action_id: str = Field(min_length=1, max_length=64)
    observation_seq: int = Field(ge=0)
    type: str = Field(min_length=1, max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)


class AgentEventAckRequest(BaseModel):
    event_cursor: int = Field(ge=0)


class AgentNpcChatTurnRequest(BaseModel):
    turn_id: str = Field(min_length=1, max_length=64)
    observation_seq: int = Field(ge=0)
    resident_slug: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=MAX_NPC_CHAT_TEXT_CHARS)
    context: str | None = Field(default=None, max_length=MAX_NPC_CHAT_CONTEXT_CHARS)


class ViewerSessionRequest(BaseModel):
    view_token: str = Field(min_length=1, max_length=512)


def _action_request_hash(body: AgentActionRequest) -> str:
    canonical = json.dumps(
        {
            "observation_seq": body.observation_seq,
            "type": body.type,
            "params": body.params,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(canonical) > MAX_ACTION_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Agent action payload is too large")
    return hashlib.sha256(canonical).hexdigest()


def _npc_chat_request_hash(body: AgentNpcChatTurnRequest) -> str:
    canonical = json.dumps(
        {
            "observation_seq": body.observation_seq,
            "resident_slug": body.resident_slug,
            "text": body.text,
            "context": body.context,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(canonical) > MAX_NPC_CHAT_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="NPC chat payload is too large")
    return hashlib.sha256(canonical).hexdigest()


def _chat_receipt_response(receipt: AgentNpcChatTurnReceipt) -> dict[str, Any]:
    return dict(receipt.response_json or {})


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _clear_profile_operation(profile: AgentPlayer, token: str | None = None) -> bool:
    if token is not None and profile.operation_token != token:
        return False
    profile.operation_kind = None
    profile.operation_token = None
    profile.operation_expires_at = None
    return True


def _assert_no_active_operation(profile: AgentPlayer) -> None:
    expires_at = _aware_utc(profile.operation_expires_at)
    if profile.operation_token and expires_at and expires_at > datetime.now(UTC):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "agent_operation_in_progress",
                "operation": profile.operation_kind,
            },
        )
    if profile.operation_token:
        _clear_profile_operation(profile)


@router.post("/agent-applications", status_code=201)
@limiter.limit("10/minute")
async def create_application(
    request: Request,
    body: AgentApplicationRequest,
    db: AsyncSession = Depends(get_db),
):
    if not (settings.debug or settings.agent_self_registration_enabled):
        raise HTTPException(
            status_code=403,
            detail="Agent self-registration is disabled on this deployment",
        )
    role_bytes = json.dumps(
        body.role_card,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(role_bytes) > MAX_ROLE_CARD_BYTES:
        raise HTTPException(status_code=413, detail="Role card is too large")
    try:
        profile, _user, resident, pair = await register_agent_player(
            db,
            name=body.display_name,
            sprite_key=body.sprite_key,
            model_label=body.model_label,
            client=body.client.model_dump(),
            role=body.role_card,
            public_visible=body.public_visible,
        )
    except AgentPlayerError as exc:
        _fail(exc)
    return {
        "application_id": profile.id,
        "status": "approved",
        "agent_status": "pending_pairing",
        "next_step": "redeem_pairing_code",
        "pairing_code": pair.plaintext,
        "expires_at": pair.credential.expires_at,
        "agent": {
            "id": profile.id,
            "slug": resident.slug,
            "name": resident.name,
            "resident_type": resident.resident_type,
            "reply_mode": resident.reply_mode,
        },
    }


@router.post("/agent-pairings/redeem")
@limiter.limit("20/minute")
async def exchange_pairing(
    request: Request,
    body: PairingRedeemRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        profile, play, view = await redeem_pairing(
            db, body.pairing_code, body.application_id
        )
    except AgentPlayerError as exc:
        _fail(exc)
    me = await agent_me(db, profile)
    return {
        "agent_token": play.plaintext,
        "viewer_token": view.plaintext,
        "agent": me["agent"],
    }


@router.post("/agent-sessions")
@limiter.limit("30/minute")
async def create_agent_session(
    request: Request,
    body: AgentSessionRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        credential, profile = await resolve_opaque_credential(db, _bearer(request), "play")
        session_token, expires_at = create_agent_session_token(profile, credential)
        profile.last_seen_at = datetime.now(UTC)
        await db.commit()
        await _refresh_agent_presence(db, profile)
    except AgentPlayerError as exc:
        _fail(exc)
    return {
        "session_token": session_token,
        "expires_at": expires_at,
        "agent": (await agent_me(db, profile))["agent"],
    }


@router.get("/agent/me")
@limiter.limit("120/minute")
async def get_agent_me(request: Request, db: AsyncSession = Depends(get_db)):
    profile = await _agent_profile(request, db)
    return await agent_me(db, profile)


@router.post("/agent/daily-reward")
@limiter.limit("10/minute")
async def claim_agent_daily_reward(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Give an external Agent the same once-per-day play budget as a human."""
    profile = await _agent_profile(request, db)
    return await claim_daily_reward(db, profile.user_id)


@router.get("/agent/observation")
@limiter.limit("120/minute")
async def get_agent_observation(request: Request, db: AsyncSession = Depends(get_db)):
    profile = await _agent_profile(request, db)
    return await observation(db, profile)


@router.post("/agent/events/ack")
@limiter.limit("120/minute")
async def acknowledge_agent_events(
    request: Request,
    body: AgentEventAckRequest,
    db: AsyncSession = Depends(get_db),
):
    profile = await _agent_profile(request, db)
    try:
        result = await acknowledge_private_events(db, profile, body.event_cursor)
    except AgentPlayerError as exc:
        _fail(exc)
    await db.commit()
    return result


@router.post("/agent/actions")
@limiter.limit("120/minute")
async def agent_action(
    request: Request,
    body: AgentActionRequest,
    db: AsyncSession = Depends(get_db),
):
    profile = await _agent_profile(request, db)
    profile_id = profile.id
    # Serialize actions for one avatar. Player messages also update the target
    # inbox sequence, so lock both profiles in deterministic ID order to avoid
    # A->B / B->A deadlocks on PostgreSQL.
    profile_ids = {profile.id}
    if body.type == "message_player":
        target_slug = next(
            (
                body.params.get(key).strip()
                for key in ("player_slug", "target_slug", "slug")
                if isinstance(body.params.get(key), str)
                and body.params.get(key).strip()
            ),
            None,
        )
        if target_slug:
            target_profile_id = (
                await db.execute(
                    select(AgentPlayer.id)
                    .join(Resident, Resident.id == AgentPlayer.resident_id)
                    .where(
                        Resident.slug == target_slug,
                        AgentPlayer.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if target_profile_id:
                profile_ids.add(target_profile_id)
    await db.execute(
        select(AgentPlayer.id)
        .where(AgentPlayer.id.in_(sorted(profile_ids)))
        .order_by(AgentPlayer.id)
        .with_for_update()
    )
    await db.refresh(profile)
    request_hash = _action_request_hash(body)
    existing = (
        await db.execute(
            select(AgentActionReceipt).where(
                AgentActionReceipt.agent_player_id == profile.id,
                AgentActionReceipt.action_id == body.action_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(
                status_code=409,
                detail={"code": "idempotency_conflict"},
            )
        return existing.result_json
    _assert_no_active_operation(profile)
    if body.observation_seq != profile.observation_seq:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_observation",
                "expected": profile.observation_seq,
                "received": body.observation_seq,
            },
        )
    try:
        result = await perform_action(db, profile, body.type, body.params)
    except AgentPlayerError as exc:
        _fail(exc)

    profile.observation_seq += 1
    presence = result.pop("_presence", None)
    response = {
        "action_id": body.action_id,
        "status": "completed",
        "result": result,
        "observation_seq": profile.observation_seq,
    }
    db.add(
        AgentActionReceipt(
            agent_player_id=profile.id,
            action_id=body.action_id,
            action_type=body.type,
            observation_seq=body.observation_seq,
            request_hash=request_hash,
            result_json=response,
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent retry may have committed the same action ID first. All
        # action side effects are in this transaction, so rollback is safe and
        # the winner's durable receipt is authoritative.
        await db.rollback()
        raced = (
            await db.execute(
                select(AgentActionReceipt).where(
                    AgentActionReceipt.agent_player_id == profile_id,
                    AgentActionReceipt.action_id == body.action_id,
                )
            )
        ).scalar_one_or_none()
        if raced is not None and raced.request_hash == request_hash:
            return raced.result_json
        raise
    # Presence is an eventually-consistent realtime projection. The durable
    # position + sequence + receipt above are authoritative and atomic.
    if presence:
        from app.ws.manager import manager

        try:
            await manager.update_agent_position(
                presence["user_id"],
                float(presence["x"]),
                float(presence["y"]),
                presence["direction"],
                presence["name"],
                ttl_seconds=settings.agent_presence_ttl_seconds,
            )
            await manager.broadcast(
                {
                    "type": "player_moved",
                    "player_id": presence["user_id"],
                    "name": presence["name"],
                    "x": presence["x"],
                    "y": presence["y"],
                    "direction": presence["direction"],
                    "agent_controlled": True,
                    "presence_ttl_seconds": settings.agent_presence_ttl_seconds,
                },
                exclude=presence["user_id"],
            )
        except Exception:
            logger.warning("Agent movement presence projection failed", exc_info=True)
    return response


@router.post("/agent/npc-chat-turns")
@limiter.limit("20/minute")
async def agent_npc_chat_turn(
    request: Request,
    body: AgentNpcChatTurnRequest,
    db: AsyncSession = Depends(get_db),
):
    """One bounded Agent->NPC exchange with durable retry semantics.

    The request first claims a receipt and persists a Conversation plus the
    user message. The LLM then runs without a DB connection. Final charging,
    assistant message, NPC cleanup, observation advance and replay response
    commit atomically. A crash leaves a recoverable pending receipt; retrying
    the same turn resumes it without opening or charging a second conversation.
    """
    profile = await _agent_profile(request, db)
    profile_id = profile.id
    user_id = profile.user_id
    request_hash = _npc_chat_request_hash(body)
    cleaned_text = body.text.strip()
    cleaned_context = body.context.strip() if body.context else None
    if not cleaned_text:
        raise HTTPException(status_code=422, detail="text is required")

    owned_lease_token: str | None = None
    redis_lock_acquired = False

    # Phase 1: serialize this avatar and claim/recover a durable turn.
    await db.refresh(profile, with_for_update=True)
    receipt = (
        await db.execute(
            select(AgentNpcChatTurnReceipt).where(
                AgentNpcChatTurnReceipt.agent_player_id == profile.id,
                AgentNpcChatTurnReceipt.turn_id == body.turn_id,
            )
        )
    ).scalar_one_or_none()
    if receipt is not None:
        if receipt.request_hash != request_hash:
            raise HTTPException(
                status_code=409, detail={"code": "idempotency_conflict"}
            )
        if receipt.status == "completed":
            return _chat_receipt_response(receipt)
        if receipt.status == "failed":
            raise HTTPException(
                status_code=receipt.http_status,
                detail=(receipt.response_json or {}).get("detail", "NPC chat failed"),
            )
        now = datetime.now(UTC)
        lease_expires_at = _aware_utc(receipt.lease_expires_at)
        if lease_expires_at is not None and lease_expires_at > now:
            raise HTTPException(status_code=409, detail={"code": "turn_in_progress"})
        _assert_no_active_operation(profile)
        if await user_over_budget(db, profile.user_id):
            raise HTTPException(status_code=429, detail="Daily chat budget exceeded")
        resident = (
            await db.execute(
                select(Resident)
                .where(Resident.id == receipt.resident_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        conversation = await db.get(Conversation, receipt.conversation_id)
        user_message = (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == receipt.conversation_id,
                    Message.role == "user",
                )
                .order_by(Message.created_at, Message.id)
            )
        ).scalars().first()
        if resident is None or conversation is None or user_message is None:
            raise HTTPException(status_code=409, detail={"code": "turn_recovery_failed"})
        if resident.status in {"sleeping", "socializing"}:
            raise HTTPException(
                status_code=409,
                detail={"code": "resident_unavailable", "status": resident.status},
            )
        recovery = receipt.recovery_json or {}
        prompt = await build_single_turn_prompt(
            db,
            resident=resident,
            user_id=profile.user_id,
            text=user_message.content,
            context=recovery.get("context"),
        )
        owned_lease_token = secrets.token_hex(16)
        lock_owner = npc_chat_lock_owner(profile.id, owned_lease_token)
        try:
            redis_lock_acquired = await manager.lock_resident(
                resident.id,
                lock_owner,
                ttl_seconds=NPC_CHAT_LEASE_SECONDS,
            )
        except Exception:
            logger.warning("Agent NPC chat lock service failed", exc_info=True)
            raise HTTPException(status_code=503, detail="NPC lock service unavailable")
        if not redis_lock_acquired:
            raise HTTPException(
                status_code=409, detail={"code": "resident_unavailable"}
            )
        resident.status = "chatting"
        receipt.lease_token = owned_lease_token
        receipt.lease_expires_at = now + timedelta(seconds=NPC_CHAT_LEASE_SECONDS)
        receipt.updated_at = now
        profile.operation_kind = "npc_chat_turn"
        profile.operation_token = owned_lease_token
        profile.operation_expires_at = receipt.lease_expires_at
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            await manager.unlock_resident(
                resident.id, expected_owner=lock_owner
            )
            raise
        # Reusing a pending receipt is safe: no durable charge or assistant
        # message exists yet. A prior model attempt may be repeated after a
        # process crash, but it cannot duplicate gameplay/economy side effects.
    else:
        _assert_no_active_operation(profile)
        if body.observation_seq != profile.observation_seq:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "stale_observation",
                    "expected": profile.observation_seq,
                    "received": body.observation_seq,
                },
            )
        resident = (
            await db.execute(
                select(Resident)
                .where(Resident.slug == body.resident_slug)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if resident is None or not resident.is_autonomous:
            raise HTTPException(status_code=404, detail="NPC resident not found")
        user = await db.get(User, profile.user_id)
        avatar = await db.get(Resident, profile.resident_id)
        if user is None or avatar is None:
            raise HTTPException(status_code=500, detail="agent identity is incomplete")
        distance = abs(resident.tile_x - int(user.last_x // 32)) + abs(
            resident.tile_y - int(user.last_y // 32)
        )
        if distance > NPC_CHAT_DISTANCE_TILES:
            raise HTTPException(status_code=422, detail="NPC resident is too far away")
        if resident.status in {"sleeping", "chatting", "socializing"}:
            raise HTTPException(
                status_code=409,
                detail={"code": "resident_unavailable", "status": resident.status},
            )
        # The synthetic Agent account starts at 0 SC; interaction is charged by
        # the same NPC turn price as human chat. Reject before a billable model
        # call when the balance cannot cover it.
        if int(user.soul_coin_balance or 0) < int(resident.token_cost_per_turn or 0):
            raise HTTPException(status_code=402, detail="Insufficient Soul Coins")
        if await user_over_budget(db, profile.user_id):
            raise HTTPException(status_code=429, detail="Daily chat budget exceeded")
        prompt = await build_single_turn_prompt(
            db,
            resident=resident,
            user_id=profile.user_id,
            text=cleaned_text,
            context=cleaned_context,
        )
        # Prompt helpers are forbidden from committing, but reacquire and
        # revalidate both rows here as a defense against future helper changes.
        await db.refresh(profile, with_for_update=True)
        _assert_no_active_operation(profile)
        if body.observation_seq != profile.observation_seq:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "stale_observation",
                    "expected": profile.observation_seq,
                    "received": body.observation_seq,
                },
            )
        await db.refresh(resident, with_for_update=True)
        if resident.status in {"sleeping", "chatting", "socializing"}:
            raise HTTPException(
                status_code=409,
                detail={"code": "resident_unavailable", "status": resident.status},
            )
        owned_lease_token = secrets.token_hex(16)
        lock_owner = npc_chat_lock_owner(profile.id, owned_lease_token)
        try:
            redis_lock_acquired = await manager.lock_resident(
                resident.id,
                lock_owner,
                ttl_seconds=NPC_CHAT_LEASE_SECONDS,
            )
        except Exception:
            logger.warning("Agent NPC chat lock service failed", exc_info=True)
            raise HTTPException(status_code=503, detail="NPC lock service unavailable")
        if not redis_lock_acquired:
            raise HTTPException(
                status_code=409, detail={"code": "resident_unavailable"}
            )
        conversation = Conversation(
            user_id=profile.user_id,
            resident_id=resident.id,
            turns=1,
        )
        db.add(conversation)
        await db.flush()
        db.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content=cleaned_text,
            )
        )
        resident.status = "chatting"
        receipt = AgentNpcChatTurnReceipt(
            agent_player_id=profile.id,
            resident_id=resident.id,
            conversation_id=conversation.id,
            turn_id=body.turn_id,
            status="pending",
            http_status=202,
            observation_seq=body.observation_seq,
            request_hash=request_hash,
            response_json={"status": "pending"},
            recovery_json={"context": cleaned_context},
            lease_token=owned_lease_token,
            lease_expires_at=datetime.now(UTC)
            + timedelta(seconds=NPC_CHAT_LEASE_SECONDS),
            updated_at=datetime.now(UTC),
        )
        profile.operation_kind = "npc_chat_turn"
        profile.operation_token = owned_lease_token
        profile.operation_expires_at = receipt.lease_expires_at
        db.add(receipt)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            await manager.unlock_resident(
                resident.id, expected_owner=lock_owner
            )
            raced = (
                await db.execute(
                    select(AgentNpcChatTurnReceipt).where(
                        AgentNpcChatTurnReceipt.agent_player_id == profile_id,
                        AgentNpcChatTurnReceipt.turn_id == body.turn_id,
                    )
                )
            ).scalar_one_or_none()
            if raced is not None and raced.request_hash == request_hash:
                if raced.status == "completed":
                    return _chat_receipt_response(raced)
                raise HTTPException(status_code=409, detail={"code": "turn_in_progress"})
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
        except Exception:
            await db.rollback()
            await manager.unlock_resident(
                resident.id, expected_owner=lock_owner
            )
            raise

    # Phase 2: no DB connection is held across the billable model call.
    assert owned_lease_token is not None
    try:
        # The Anthropic SDK applies its timeout per retry attempt. Bound the
        # entire operation so automatic retries can never outlive the durable
        # receipt and shared resident leases.
        async with asyncio.timeout(NPC_CHAT_CALL_TIMEOUT_SECONDS):
            reply = await generate_single_turn_reply(
                prompt=prompt,
                resident_id=resident.id,
                user_id=user_id,
                conversation_id=conversation.id,
            )
        if not reply:
            raise RuntimeError("NPC returned an empty reply")
    except Exception:
        logger.warning("Agent NPC chat model call failed", exc_info=True)
        # Release our own lease and restore availability immediately. The same
        # turn ID can resume; no charge or assistant message has committed.
        failed_profile = await db.get(AgentPlayer, profile_id)
        failed_receipt = None
        failed_resident = None
        owns_shared_lock = False
        ownership_checked = False
        if failed_profile is not None:
            await db.refresh(failed_profile, with_for_update=True)
            failed_receipt = (
                await db.execute(
                    select(AgentNpcChatTurnReceipt)
                    .where(AgentNpcChatTurnReceipt.id == receipt.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
        if failed_receipt is not None:
            if (
                failed_receipt.status == "pending"
                and failed_receipt.lease_token == owned_lease_token
                and failed_profile.operation_token == owned_lease_token
            ):
                failed_resident = (
                    await db.execute(
                        select(Resident)
                        .where(Resident.id == failed_receipt.resident_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                try:
                    owns_shared_lock = await manager.lock_resident(
                        failed_receipt.resident_id,
                        lock_owner,
                        ttl_seconds=30,
                    )
                    ownership_checked = True
                except Exception:
                    logger.warning(
                        "Agent NPC failure lock ownership check failed",
                        exc_info=True,
                    )
                if not ownership_checked:
                    await db.rollback()
                elif owns_shared_lock and failed_resident is not None:
                    failed_resident.status = (
                        "popular" if failed_resident.heat >= 50 else "idle"
                    )
                if ownership_checked:
                    failed_receipt.lease_token = None
                    failed_receipt.lease_expires_at = None
                    failed_receipt.updated_at = datetime.now(UTC)
                    _clear_profile_operation(failed_profile, owned_lease_token)
                    await db.commit()
        if owns_shared_lock and failed_resident is not None:
            await release_npc_chat_lock_and_notify(
                agent_player_id=profile_id,
                lease_token=owned_lease_token,
                resident_id=failed_resident.id,
                resident_slug=failed_resident.slug,
                resident_name=failed_resident.name,
            )
            await manager.broadcast(
                {
                    "type": "resident_status",
                    "resident_slug": failed_resident.slug,
                    "status": failed_resident.status,
                    "mood_label": failed_resident.mood_label,
                }
            )
        raise HTTPException(
            status_code=503,
            detail={"code": "npc_chat_temporarily_unavailable", "retry_same_turn_id": True},
        )

    # Phase 3: claim the still-pending receipt and atomically finalize effects.
    try:
        lock_still_owned = await manager.lock_resident(
            resident.id,
            lock_owner,
            ttl_seconds=60,
        )
    except Exception:
        logger.warning("Agent NPC finalization lock refresh failed", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={"code": "npc_chat_temporarily_unavailable", "retry_same_turn_id": True},
        )
    profile = await db.get(AgentPlayer, profile_id)
    if profile is None:
        if lock_still_owned:
            await manager.unlock_resident(
                resident.id, expected_owner=lock_owner
            )
        raise HTTPException(status_code=409, detail={"code": "turn_recovery_failed"})
    await db.refresh(profile, with_for_update=True)
    receipt = (
        await db.execute(
            select(AgentNpcChatTurnReceipt)
            .where(AgentNpcChatTurnReceipt.id == receipt.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if receipt is None:
        if lock_still_owned:
            await manager.unlock_resident(
                resident.id, expected_owner=lock_owner
            )
        raise HTTPException(status_code=409, detail={"code": "turn_recovery_failed"})
    if receipt.request_hash != request_hash:
        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
    if receipt.status == "completed":
        if lock_still_owned:
            await manager.unlock_resident(
                resident.id, expected_owner=lock_owner
            )
        return _chat_receipt_response(receipt)
    lease_expires_at = _aware_utc(receipt.lease_expires_at)
    lease_valid = (
        lock_still_owned
        and receipt.lease_token == owned_lease_token
        and profile.operation_token == owned_lease_token
        and lease_expires_at is not None
        and lease_expires_at > datetime.now(UTC)
    )
    resident = (
        await db.execute(
            select(Resident)
            .where(Resident.id == receipt.resident_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    conversation = await db.get(Conversation, receipt.conversation_id)
    if resident is None or conversation is None:
        raise HTTPException(status_code=409, detail={"code": "turn_recovery_failed"})
    if not lease_valid:
        if receipt.lease_token == owned_lease_token:
            receipt.lease_token = None
            receipt.lease_expires_at = None
            receipt.updated_at = datetime.now(UTC)
        _clear_profile_operation(profile, owned_lease_token)
        # Only our successfully renewed shared lease authorizes a DB status
        # reset; otherwise another chat owner may already be active.
        if lock_still_owned:
            resident.status = "popular" if resident.heat >= 50 else "idle"
        await db.commit()
        if lock_still_owned:
            await release_npc_chat_lock_and_notify(
                agent_player_id=profile_id,
                lease_token=owned_lease_token,
                resident_id=resident.id,
                resident_slug=resident.slug,
                resident_name=resident.name,
            )
        raise HTTPException(status_code=409, detail={"code": "turn_lease_lost"})
    if profile.observation_seq != receipt.observation_seq:
        receipt.status = "failed"
        receipt.http_status = 409
        receipt.response_json = {"detail": {"code": "observation_changed_during_turn"}}
        receipt.lease_token = None
        receipt.lease_expires_at = None
        receipt.updated_at = datetime.now(UTC)
        _clear_profile_operation(profile, owned_lease_token)
        resident.status = "popular" if resident.heat >= 50 else "idle"
        conversation.ended_at = datetime.now(UTC)
        await db.commit()
        await release_npc_chat_lock_and_notify(
            agent_player_id=profile_id,
            lease_token=owned_lease_token,
            resident_id=resident.id,
            resident_slug=resident.slug,
            resident_name=resident.name,
        )
        raise HTTPException(status_code=409, detail={"code": "observation_changed_during_turn"})

    charged = await charge_pending(
        db,
        profile.user_id,
        int(resident.token_cost_per_turn),
        f"chat:{resident.slug}",
    )
    if not charged:
        receipt.status = "failed"
        receipt.http_status = 402
        receipt.response_json = {"detail": "Insufficient Soul Coins"}
        receipt.lease_token = None
        receipt.lease_expires_at = None
        receipt.updated_at = datetime.now(UTC)
        _clear_profile_operation(profile, owned_lease_token)
        resident.status = "popular" if resident.heat >= 50 else "idle"
        conversation.ended_at = datetime.now(UTC)
        await db.commit()
        await release_npc_chat_lock_and_notify(
            agent_player_id=profile_id,
            lease_token=owned_lease_token,
            resident_id=resident.id,
            resident_slug=resident.slug,
            resident_name=resident.name,
        )
        raise HTTPException(status_code=402, detail="Insufficient Soul Coins")

    db.add(Message(conversation_id=conversation.id, role="assistant", content=reply))
    resident.status = "popular" if resident.heat >= 50 else "idle"
    resident.total_conversations += 1
    resident.last_conversation_at = datetime.now(UTC)
    if settings.realism_enabled:
        try:
            from app.agent.needs import get_needs, write_needs

            needs = get_needs(resident)
            needs["social"] += settings.realism_social_greet
            write_needs(resident, needs)
        except Exception:
            logger.warning("Agent NPC chat social restore failed", exc_info=True)
    conversation.ended_at = datetime.now(UTC)
    profile.observation_seq += 1
    _clear_profile_operation(profile, owned_lease_token)
    balance = await get_balance(db, profile.user_id)
    response = {
        "turn_id": body.turn_id,
        "status": "completed",
        "resident": {"slug": resident.slug, "name": resident.name},
        "reply": reply,
        "conversation_id": conversation.id,
        "charged_sc": int(resident.token_cost_per_turn),
        "balance": balance,
        "observation_seq": profile.observation_seq,
    }
    receipt.status = "completed"
    receipt.http_status = 200
    receipt.response_json = response
    receipt.recovery_json = {}
    receipt.lease_token = None
    receipt.lease_expires_at = None
    receipt.updated_at = datetime.now(UTC)
    await db.commit()
    await release_npc_chat_lock_and_notify(
        agent_player_id=profile_id,
        lease_token=owned_lease_token,
        resident_id=resident.id,
        resident_slug=resident.slug,
        resident_name=resident.name,
    )
    await manager.broadcast(
        {
            "type": "resident_status",
            "resident_slug": resident.slug,
            "status": resident.status,
            "mood_label": resident.mood_label,
        },
        exclude=profile.user_id,
    )

    # Non-critical follow-up effects happen only after the durable completed
    # receipt exists. A failure here never changes the replayed result. Creator
    # passive rewards are intentionally omitted in this first single-turn path:
    # they need their own idempotency key before they can safely run after the
    # receipt commit.
    try:
        from app.events.bus import emit

        await emit(
            None,
            "chat_completed",
            user_id=profile.user_id,
            resident_id=resident.id,
            turns=1,
            conversation_id=conversation.id,
        )
    except Exception:
        logger.warning("Agent NPC chat completion event failed", exc_info=True)
    if settings.realism_relations_enabled:
        try:
            from app.services import relation_service

            await relation_service.bump(
                db,
                resident.id,
                profile.user_id,
                d_familiarity=settings.realism_rel_familiarity_chat,
                type1="resident",
                type2="player",
            )
        except Exception:
            logger.warning("Agent NPC chat relation bump failed", exc_info=True)
    user = await db.get(User, profile.user_id)
    asyncio.create_task(
        extract_player_chat_memories(
            resident_id=resident.id,
            user_id=profile.user_id,
            user_name=user.name if user else "玩家",
            chat_messages=[
                {"role": "user", "content": cleaned_text},
                {"role": "assistant", "content": reply},
            ],
        )
    )
    return response


@router.get("/public/town/snapshot")
@limiter.limit("120/minute")
async def town_snapshot(request: Request, db: AsyncSession = Depends(get_db)):
    return await public_town_snapshot(db)


@router.post("/viewer/sessions")
@limiter.limit("30/minute")
async def create_viewer_session(
    request: Request,
    response: Response,
    body: ViewerSessionRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        credential, profile = await resolve_opaque_credential(db, body.view_token, "view")
        session_token, _expires_at = create_viewer_session_token(profile, credential)
    except AgentPlayerError as exc:
        _fail(exc)
    response.set_cookie(
        "sv_viewer_session",
        session_token,
        max_age=settings.agent_viewer_session_minutes * 60,
        httponly=True,
        secure=not settings.debug,
        # The hosted frontend and API are commonly different sites
        # (Cloudflare Workers -> api.example.com), so a cross-site credentialed
        # fetch needs SameSite=None in production. Debug remains Strict and may
        # use plain loopback HTTP.
        samesite="strict" if settings.debug else "none",
        path="/api/v1/viewer",
    )
    return {"ok": True}


@router.get("/viewer/snapshot")
@limiter.limit("120/minute")
async def get_viewer_snapshot(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    token = request.cookies.get("sv_viewer_session")
    if not token:
        raise HTTPException(status_code=401, detail="Missing viewer session")
    try:
        profile = await require_viewer_session(db, token)
    except AgentPlayerError as exc:
        _fail(exc)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    return await viewer_snapshot(db, profile)


@router.delete("/viewer/sessions")
async def delete_viewer_session(response: Response):
    """End a read-only viewing session on a shared browser."""
    response.delete_cookie(
        "sv_viewer_session",
        httponly=True,
        secure=not settings.debug,
        samesite="strict" if settings.debug else "none",
        path="/api/v1/viewer",
    )
    return {"ok": True}
