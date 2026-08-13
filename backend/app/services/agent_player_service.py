"""Registration, scoped authentication and game actions for external players."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.map_data import LOCATIONS, get_location_id_at
from app.agent.pathfinder import find_path, get_walkable_tiles
from app.config import settings
from app.models.agent_player import (
    AgentCredential,
    AgentEvent,
    AgentPlayer,
)
from app.models.resident import Resident
from app.models.user import User
from app.services.onboarding_service import TILE_SIZE, create_player_resident
from app.services.resident_placement import SPRITE_KEYS
from app.world_clock import now_world
from app.ws.manager import manager


class AgentPlayerError(ValueError):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class IssuedToken:
    plaintext: str
    credential: AgentCredential


PLAYER_MESSAGE_DISTANCE_TILES = 2


def _credential_hash(token: str) -> str:
    """Server-peppered digest; opaque token plaintext never reaches the DB."""
    return hmac.new(
        settings.jwt_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _derived_jwt_secret(purpose: str) -> bytes:
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        f"simverse:{purpose}:v1".encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _new_opaque(kind: str) -> str:
    return f"sv_{kind}_{secrets.token_urlsafe(32)}"


def _new_credential(
    agent_player_id: str,
    kind: str,
    *,
    expires_at: datetime | None = None,
) -> IssuedToken:
    plaintext = _new_opaque(kind)
    credential = AgentCredential(
        agent_player_id=agent_player_id,
        kind=kind,
        token_hash=_credential_hash(plaintext),
        token_prefix=plaintext[:16],
        expires_at=expires_at,
    )
    return IssuedToken(plaintext=plaintext, credential=credential)


def _utc_aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _agent_is_online(profile: AgentPlayer) -> bool:
    last_seen = _utc_aware(profile.last_seen_at)
    if last_seen is None:
        return False
    return last_seen >= datetime.now(UTC) - timedelta(
        seconds=max(1, settings.agent_presence_ttl_seconds)
    )


def _tile_from_presence(position: dict[str, Any] | None) -> tuple[int, int] | None:
    if not isinstance(position, dict):
        return None
    try:
        x = float(position["x"])
        y = float(position["y"])
    except (KeyError, TypeError, ValueError):
        return None
    return int(x // TILE_SIZE), int(y // TILE_SIZE)


async def _visible_player_tiles_by_user_id() -> dict[str, tuple[int, int]]:
    visible: dict[str, tuple[int, int]] = {}
    for player in await manager.get_online_players():
        user_id = player.get("player_id")
        if not isinstance(user_id, str) or not user_id:
            continue
        coords = _tile_from_presence(player)
        if coords is not None:
            visible[user_id] = coords
    return visible


def _viewer_location(location_id: str | None) -> dict[str, str] | None:
    if not location_id:
        return None
    location = LOCATIONS.get(location_id) or {}
    name = location.get("name")
    return {"slug": location_id, "name": str(name) if name else location_id}


async def register_agent_player(
    db: AsyncSession,
    *,
    name: str,
    sprite_key: str,
    model_label: str | None,
    client: dict[str, Any],
    role: dict[str, Any],
    public_visible: bool,
) -> tuple[AgentPlayer, User, Resident, IssuedToken]:
    """Atomically create an Agent principal, player avatar and pairing code."""
    cleaned_name = name.strip()
    if not cleaned_name:
        raise AgentPlayerError("name is required")
    if sprite_key not in SPRITE_KEYS:
        raise AgentPlayerError("unknown sprite_key")

    from app.services.content_guard import assert_resident_content_clean

    ability_md = str(role.get("ability_md") or "")
    persona_md = str(role.get("persona_md") or "")
    soul_md = str(role.get("soul_md") or "")
    assert_resident_content_clean(
        name=cleaned_name,
        ability_md=ability_md,
        persona_md=persona_md,
        soul_md=soul_md,
    )

    nonce = uuid.uuid4().hex
    user = User(
        name=cleaned_name,
        email=f"agent+{nonce}@agents.simverse.invalid",
        hashed_password=None,
        soul_coin_balance=0,
        settings_json={"principal_kind": "external_agent"},
    )
    db.add(user)
    try:
        await db.flush()
        resident = await create_player_resident(
            db,
            user.id,
            cleaned_name,
            sprite_key,
            reply_mode="manual",
            ability_md=ability_md,
            persona_md=persona_md,
            soul_md=soul_md,
            commit=False,
        )
        # Make the external origin visible to public projections while retaining
        # resident_type=player so the NPC autonomy loop can never pick it up.
        resident.meta_json = {
            **(resident.meta_json or {}),
            "origin": "external_agent",
            "agent_controlled": True,
        }
        profile = AgentPlayer(
            user_id=user.id,
            resident_id=resident.id,
            control_kind="external_agent",
            model_label=(model_label or "").strip()[:100] or None,
            client_json=client,
            role_json=role,
            # The avatar is not part of the running/public town until the
            # one-time pairing code is redeemed. Lost/expired applications
            # therefore cannot leave active, publicly visible orphan players.
            status="pending_pairing",
            public_visible=public_visible,
        )
        db.add(profile)
        await db.flush()

        pair = _new_credential(
            profile.id,
            "pair",
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.agent_pairing_minutes),
        )
        db.add(pair.credential)
        await db.commit()
        await db.refresh(profile)
        return profile, user, resident, pair
    except Exception:
        await db.rollback()
        raise


async def redeem_pairing(
    db: AsyncSession, pairing_code: str, expected_agent_player_id: str | None = None
) -> tuple[AgentPlayer, IssuedToken, IssuedToken]:
    now = datetime.now(UTC)
    token_hash = _credential_hash(pairing_code)
    credential = (
        await db.execute(
            select(AgentCredential).where(
                AgentCredential.token_hash == token_hash,
                AgentCredential.kind == "pair",
            )
        )
    ).scalar_one_or_none()
    if credential is None or credential.revoked_at is not None:
        raise AgentPlayerError("invalid pairing code", 401)
    if expected_agent_player_id and credential.agent_player_id != expected_agent_player_id:
        raise AgentPlayerError("pairing code does not match application", 401)
    if _utc_aware(credential.expires_at) and _utc_aware(credential.expires_at) <= now:
        raise AgentPlayerError("pairing code expired", 401)

    # Conditional claim makes the one-time exchange safe under concurrent calls.
    claim = await db.execute(
        update(AgentCredential)
        .where(AgentCredential.id == credential.id, AgentCredential.used_at.is_(None))
        .values(used_at=now)
    )
    if claim.rowcount != 1:
        await db.rollback()
        raise AgentPlayerError("pairing code already used", 409)

    profile = await db.get(AgentPlayer, credential.agent_player_id)
    if profile is None or profile.status != "pending_pairing":
        await db.rollback()
        raise AgentPlayerError("agent application is not awaiting pairing", 403)
    profile.status = "active"
    play = _new_credential(profile.id, "play")
    view = _new_credential(profile.id, "view")
    db.add_all([play.credential, view.credential])
    await db.commit()
    return profile, play, view


async def resolve_opaque_credential(
    db: AsyncSession, token: str, kind: str
) -> tuple[AgentCredential, AgentPlayer]:
    if not token.startswith(f"sv_{kind}_"):
        raise AgentPlayerError(f"invalid {kind} token", 401)
    credential = (
        await db.execute(
            select(AgentCredential).where(
                AgentCredential.token_hash == _credential_hash(token),
                AgentCredential.kind == kind,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if credential is None or credential.revoked_at is not None:
        raise AgentPlayerError(f"invalid {kind} token", 401)
    expires_at = _utc_aware(credential.expires_at)
    if expires_at and expires_at <= now:
        raise AgentPlayerError(f"expired {kind} token", 401)
    profile = await db.get(AgentPlayer, credential.agent_player_id)
    if profile is None or profile.status != "active":
        raise AgentPlayerError("agent is not active", 403)
    user = await db.get(User, profile.user_id)
    if user is None or user.is_banned:
        raise AgentPlayerError("agent owner is unavailable", 403)
    return credential, profile


def create_agent_session_token(
    profile: AgentPlayer, credential: AgentCredential
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.agent_player_session_minutes)
    token = jwt.encode(
        {
            "iss": "simverse-agent",
            "aud": "simverse-agent-api",
            "typ": "agent_session",
            "scope": "agent:play",
            "sub": profile.user_id,
            "agent_id": profile.id,
            "credential_id": credential.id,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": expires_at,
        },
        _derived_jwt_secret("agent-session"),
        algorithm=settings.jwt_algorithm,
    )
    return token, expires_at


async def require_agent_session(db: AsyncSession, token: str) -> AgentPlayer:
    try:
        payload = jwt.decode(
            token,
            _derived_jwt_secret("agent-session"),
            algorithms=[settings.jwt_algorithm],
            issuer="simverse-agent",
            audience="simverse-agent-api",
            options={
                "require": [
                    "exp", "sub", "agent_id", "scope", "credential_id", "jti", "aud"
                ]
            },
        )
    except Exception as exc:
        raise AgentPlayerError("invalid or expired agent session", 401) from exc
    if payload.get("typ") != "agent_session" or payload.get("scope") != "agent:play":
        raise AgentPlayerError("invalid agent session scope", 403)
    profile = await db.get(AgentPlayer, payload["agent_id"])
    source = await db.get(AgentCredential, payload["credential_id"])
    user = await db.get(User, profile.user_id) if profile is not None else None
    if (
        profile is None
        or profile.status != "active"
        or profile.user_id != payload.get("sub")
        or source is None
        or source.kind != "play"
        or source.agent_player_id != profile.id
        or source.revoked_at is not None
        or user is None
        or user.is_banned
        or (_utc_aware(source.expires_at) and _utc_aware(source.expires_at) <= datetime.now(UTC))
    ):
        raise AgentPlayerError("agent is not active", 403)
    return profile


def create_viewer_session_token(
    profile: AgentPlayer, credential: AgentCredential
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.agent_viewer_session_minutes)
    token = jwt.encode(
        {
            "iss": "simverse-viewer",
            "aud": "simverse-viewer-api",
            "typ": "viewer_session",
            "scope": "agent:view",
            "agent_id": profile.id,
            "credential_id": credential.id,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": expires_at,
        },
        _derived_jwt_secret("viewer-session"),
        algorithm=settings.jwt_algorithm,
    )
    return token, expires_at


async def require_viewer_session(db: AsyncSession, token: str) -> AgentPlayer:
    try:
        payload = jwt.decode(
            token,
            _derived_jwt_secret("viewer-session"),
            algorithms=[settings.jwt_algorithm],
            issuer="simverse-viewer",
            audience="simverse-viewer-api",
            options={
                "require": ["exp", "agent_id", "scope", "credential_id", "jti", "aud"]
            },
        )
    except Exception as exc:
        raise AgentPlayerError("invalid or expired viewer session", 401) from exc
    if payload.get("typ") != "viewer_session" or payload.get("scope") != "agent:view":
        raise AgentPlayerError("invalid viewer session scope", 403)
    profile = await db.get(AgentPlayer, payload["agent_id"])
    source = await db.get(AgentCredential, payload["credential_id"])
    user = await db.get(User, profile.user_id) if profile is not None else None
    if (
        profile is None
        or profile.status != "active"
        or source is None
        or source.kind != "view"
        or source.agent_player_id != profile.id
        or source.revoked_at is not None
        or user is None
        or user.is_banned
        or (_utc_aware(source.expires_at) and _utc_aware(source.expires_at) <= datetime.now(UTC))
    ):
        raise AgentPlayerError("agent is not active", 403)
    return profile


async def _profile_entities(
    db: AsyncSession, profile: AgentPlayer
) -> tuple[User, Resident]:
    user = await db.get(User, profile.user_id)
    resident = await db.get(Resident, profile.resident_id)
    if user is None or resident is None:
        raise AgentPlayerError("agent identity is incomplete", 500)
    return user, resident


def _public_agent(profile: AgentPlayer, resident: Resident) -> dict[str, Any]:
    return {
        "id": profile.id,
        "control_kind": profile.control_kind,
        "model_label": profile.model_label,
        "resident": {
            "id": resident.id,
            "slug": resident.slug,
            "name": resident.name,
            "sprite_key": resident.sprite_key,
            "resident_type": resident.resident_type,
            "agent_controlled": True,
        },
    }


async def agent_me(db: AsyncSession, profile: AgentPlayer) -> dict[str, Any]:
    user, resident = await _profile_entities(db, profile)
    tile_x, tile_y = int(user.last_x // TILE_SIZE), int(user.last_y // TILE_SIZE)
    return {
        "agent": _public_agent(profile, resident),
        "status": profile.status,
        "observation_seq": profile.observation_seq,
        "reply_mode": resident.reply_mode,
        "balance": user.soul_coin_balance,
        "position": {"tile_x": tile_x, "tile_y": tile_y},
        "location": get_location_id_at(tile_x, tile_y),
        "capabilities": [
            "observe",
            "events_ack",
            "daily_reward",
            "wait",
            "move",
            "move_to",
            "message_player",
            "npc_chat_turn",
        ],
    }


def _serialize_private_event(event: AgentEvent) -> dict[str, Any]:
    payload = event.payload_json or {}
    return {
        "seq": event.sequence,
        "kind": event.kind,
        "created_at": event.created_at.isoformat(),
        "from": {
            "slug": payload.get("from_slug"),
            "name": payload.get("from_name"),
            "model_label": payload.get("from_model_label"),
        },
        "text": str(payload.get("text") or ""),
    }


async def _recent_private_events(
    db: AsyncSession, profile: AgentPlayer
) -> tuple[list[dict[str, Any]], int, bool]:
    limit = max(1, settings.agent_observation_event_limit)
    rows = (
        await db.execute(
            select(AgentEvent)
            .where(AgentEvent.agent_player_id == profile.id)
            .where(AgentEvent.sequence > profile.last_seen_event_seq)
            .order_by(AgentEvent.sequence.asc())
            .limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    visible = rows[:limit]
    cursor = visible[-1].sequence if visible else profile.last_seen_event_seq
    return [_serialize_private_event(event) for event in visible], cursor, has_more


async def observation(
    db: AsyncSession,
    profile: AgentPlayer,
    *,
    include_private_events: bool = True,
) -> dict[str, Any]:
    user, resident = await _profile_entities(db, profile)
    sx, sy = int(user.last_x // TILE_SIZE), int(user.last_y // TILE_SIZE)
    radius = settings.agent_observation_radius_tiles
    # F3: prune non-player rows to the bbox in SQL instead of a full-table
    # scan. Player rows are exempt — their coordinates come from the live
    # presence overlay below, so the DB tile may be stale. The exact circular
    # (Manhattan) filter in Python below is kept unchanged.
    residents = (
        await db.execute(
            select(Resident).where(
                or_(
                    Resident.resident_type == "player",
                    and_(
                        Resident.tile_x.between(sx - radius, sx + radius),
                        Resident.tile_y.between(sy - radius, sy + radius),
                    ),
                )
            )
        )
    ).scalars().all()
    visible_player_tiles = await _visible_player_tiles_by_user_id()
    player_creator_ids = {
        other.creator_id
        for other in residents
        if other.resident_type == "player" and other.creator_id
    }
    eligible_player_users: set[str] = set()
    if player_creator_ids:
        eligible_player_users = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(player_creator_ids), User.is_banned.is_(False)
                    )
                )
            ).scalars().all()
        )
    controlled_states = dict(
        (
            await db.execute(
                select(AgentPlayer.resident_id, AgentPlayer.status).where(
                    AgentPlayer.resident_id.in_(
                        [
                            other.id
                            for other in residents
                            if other.resident_type == "player"
                        ]
                    )
                )
            )
        ).all()
    )
    nearby_residents: list[dict[str, Any]] = []
    nearby_players: list[dict[str, Any]] = []
    for other in residents:
        if other.id == resident.id:
            continue
        tile_x, tile_y = other.tile_x, other.tile_y
        if other.resident_type == "player":
            if other.creator_id not in eligible_player_users:
                continue
            controlled_status = controlled_states.get(other.id)
            if controlled_status is not None and controlled_status != "active":
                continue
            coords = visible_player_tiles.get(other.creator_id or "")
            if coords is None:
                continue
            tile_x, tile_y = coords
        distance = abs(tile_x - sx) + abs(tile_y - sy)
        if distance > radius:
            continue
        item = {
            "id": other.id,
            "slug": other.slug,
            "name": other.name,
            "kind": "player" if other.resident_type == "player" else "resident",
            "status": other.status,
            "tile_x": tile_x,
            "tile_y": tile_y,
            "distance_tiles": distance,
            "interactable": distance <= PLAYER_MESSAGE_DISTANCE_TILES,
        }
        (nearby_players if other.resident_type == "player" else nearby_residents).append(item)
    nearby_residents.sort(key=lambda item: (item["distance_tiles"], item["slug"]))
    nearby_players.sort(key=lambda item: (item["distance_tiles"], item["slug"]))
    recent_events: list[dict[str, Any]] = []
    event_cursor = profile.last_seen_event_seq
    has_more_events = False
    if include_private_events:
        recent_events, event_cursor, has_more_events = await _recent_private_events(
            db, profile
        )
    return {
        "observation_seq": profile.observation_seq,
        "event_cursor": event_cursor,
        "observed_at": datetime.now(UTC).isoformat(),
        "world_time": now_world().isoformat(),
        "self": {
            "agent_id": profile.id,
            "resident_id": resident.id,
            "slug": resident.slug,
            "name": resident.name,
            "tile_x": sx,
            "tile_y": sy,
            "location": get_location_id_at(sx, sy),
            "balance": user.soul_coin_balance,
        },
        "nearby": {"residents": nearby_residents, "players": nearby_players},
        "recent_events": recent_events,
        "has_more_events": has_more_events if include_private_events else False,
        "affordances": [
            {"action": "wait", "max_seconds": 60},
            {"action": "move", "max_tiles": 1},
            {"action": "move_to", "max_advance_tiles": settings.agent_move_max_tiles},
            {
                "action": "message_player",
                "max_distance_tiles": PLAYER_MESSAGE_DISTANCE_TILES,
                "max_chars": settings.agent_message_max_chars,
            },
            {
                "action": "npc_chat_turn",
                "endpoint": "/api/v1/agent/npc-chat-turns",
                "max_distance_tiles": PLAYER_MESSAGE_DISTANCE_TILES,
                "max_chars": 1000,
                "mode": "single_turn",
            },
        ],
        "limitations": {
            "npc_chat": "single_turn_only_no_media_wake_or_queue",
            "player_chat": "direct_message_to_active_agent_only",
        },
    }


async def acknowledge_private_events(
    db: AsyncSession, profile: AgentPlayer, event_cursor: int
) -> dict[str, Any]:
    await db.refresh(profile, with_for_update=True)
    if event_cursor > profile.event_seq:
        raise AgentPlayerError("event_cursor is ahead of the inbox", 422)
    previous = profile.last_seen_event_seq
    if event_cursor > previous:
        profile.last_seen_event_seq = event_cursor
        await db.flush()
    return {
        "event_cursor": profile.last_seen_event_seq,
        "acknowledged": max(0, profile.last_seen_event_seq - previous),
    }


def _target_tile(payload: dict[str, Any]) -> tuple[int, int]:
    location_id = payload.get("location_id") or payload.get("target")
    if isinstance(location_id, str) and location_id:
        location = LOCATIONS.get(location_id)
        if location is None:
            raise AgentPlayerError("unknown location_id")
        target = location.get("entrance") or location.get("center")
        if not target:
            raise AgentPlayerError("location has no reachable target")
        return int(target[0]), int(target[1])
    try:
        return int(payload["tile_x"]), int(payload["tile_y"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentPlayerError("tile_x/tile_y or location_id is required") from exc


def _target_player_slug(payload: dict[str, Any]) -> str:
    for key in ("player_slug", "target_slug", "slug"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise AgentPlayerError("player_slug is required")


def _message_text(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if not isinstance(text, str):
        raise AgentPlayerError("text is required")
    cleaned = text.strip()
    if not cleaned:
        raise AgentPlayerError("text is required")
    if len(cleaned) > settings.agent_message_max_chars:
        raise AgentPlayerError(
            f"text exceeds {settings.agent_message_max_chars} characters"
        )
    return cleaned


async def perform_action(
    db: AsyncSession, profile: AgentPlayer, action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    user, resident = await _profile_entities(db, profile)
    if action == "wait":
        seconds = payload.get("seconds", 1)
        try:
            seconds = float(seconds)
        except (TypeError, ValueError) as exc:
            raise AgentPlayerError("seconds must be numeric") from exc
        if not 0 <= seconds <= 60:
            raise AgentPlayerError("seconds must be between 0 and 60")
        # REST never blocks a worker for the requested role-play duration. The
        # caller owns scheduling and receives the accepted duration as a receipt.
        return {
            "ok": True,
            "action": "wait",
            "wait_seconds": seconds,
            "server_blocked": False,
        }

    if action == "message_player":
        target_slug = _target_player_slug(payload)
        text = _message_text(payload)
        row = (
            await db.execute(
                select(AgentPlayer, Resident)
                .join(Resident, Resident.id == AgentPlayer.resident_id)
                .join(User, User.id == AgentPlayer.user_id)
                .where(
                    Resident.slug == target_slug,
                    AgentPlayer.status == "active",
                    User.is_banned.is_(False),
                )
            )
        ).first()
        if row is None:
            raise AgentPlayerError("target player is unavailable", 404)
        target_profile, target_resident = row
        if target_profile.id == profile.id:
            raise AgentPlayerError("cannot message yourself")
        distance = abs(target_resident.tile_x - resident.tile_x) + abs(
            target_resident.tile_y - resident.tile_y
        )
        if distance > PLAYER_MESSAGE_DISTANCE_TILES:
            raise AgentPlayerError("target player is too far away", 422)
        next_event_seq = (
            await db.execute(
                update(AgentPlayer)
                .where(AgentPlayer.id == target_profile.id, AgentPlayer.status == "active")
                .values(event_seq=AgentPlayer.event_seq + 1)
                .returning(AgentPlayer.event_seq)
            )
        ).scalar_one_or_none()
        if next_event_seq is None:
            raise AgentPlayerError("target player is unavailable", 404)
        db.add(
            AgentEvent(
                agent_player_id=target_profile.id,
                sequence=int(next_event_seq),
                kind="player_message",
                payload_json={
                    "from_slug": resident.slug,
                    "from_name": resident.name,
                    "from_model_label": profile.model_label,
                    "text": text,
                },
            )
        )
        await db.flush()
        return {
            "ok": True,
            "action": "message_player",
            "recipient": {
                "slug": target_resident.slug,
                "name": target_resident.name,
            },
            "distance_tiles": distance,
            "delivery": "queued",
            "event_seq": int(next_event_seq),
        }

    if action not in {"move", "move_to"}:
        raise AgentPlayerError("unsupported action")

    start = (int(user.last_x // TILE_SIZE), int(user.last_y // TILE_SIZE))
    target = _target_tile(payload)
    walkable = get_walkable_tiles()
    if target not in walkable:
        raise AgentPlayerError("target tile is outside the walkable map", 422)
    path = find_path(start, target, walkable, max_steps=500)
    if path is None:
        raise AgentPlayerError("target tile is unreachable", 422)
    if action == "move" and len(path) > 2:
        raise AgentPlayerError("move may advance at most one tile", 422)

    max_advance = 1 if action == "move" else max(1, settings.agent_move_max_tiles)
    advance = min(max_advance, len(path) - 1)
    destination = path[advance]
    user.last_x = destination[0] * TILE_SIZE + TILE_SIZE // 2
    user.last_y = destination[1] * TILE_SIZE + TILE_SIZE // 2
    resident.tile_x, resident.tile_y = destination
    # The router persists position, observation sequence and idempotency receipt
    # in one commit. Never commit here: a crash between movement and receipt
    # would make a retried action advance twice.
    await db.flush()

    dx, dy = destination[0] - start[0], destination[1] - start[1]
    direction = "right" if dx > 0 else "left" if dx < 0 else "down" if dy > 0 else "up"
    return {
        "ok": True,
        "action": action,
        "from": {"tile_x": start[0], "tile_y": start[1]},
        "position": {"tile_x": destination[0], "tile_y": destination[1]},
        "target": {"tile_x": target[0], "tile_y": target[1]},
        "advanced_tiles": advance,
        "remaining_tiles": max(0, len(path) - 1 - advance),
        "reached": destination == target,
        "location": get_location_id_at(*destination),
        "_presence": {
            "user_id": user.id,
            "name": user.name,
            "x": user.last_x,
            "y": user.last_y,
            "direction": direction,
        },
    }


async def viewer_snapshot(db: AsyncSession, profile: AgentPlayer) -> dict[str, Any]:
    data = await observation(
        db,
        profile,
        include_private_events=False,
    )
    resident = await db.get(Resident, profile.resident_id)
    assert resident is not None
    visible_player_tiles = await _visible_player_tiles_by_user_id()
    role = profile.role_json if isinstance(profile.role_json, dict) else {}
    raw_goal = role.get("goal")
    raw_goals = role.get("goals")
    if not isinstance(raw_goal, (str, int, float, bool)):
        raw_goal = None
    if raw_goal is None and isinstance(raw_goals, dict):
        raw_goal = raw_goals.get("public")
    current_goal = (
        str(raw_goal)[:500]
        if isinstance(raw_goal, (str, int, float, bool))
        else None
    )
    online = _agent_is_online(profile) and profile.user_id in visible_player_tiles

    def spectator_actor(item: dict[str, Any], kind: str) -> dict[str, Any]:
        tile_x = item["tile_x"]
        tile_y = item["tile_y"]
        return {
            "slug": item["slug"],
            "name": item["name"],
            "kind": kind,
            "status": item["status"],
            "district": (
                get_location_id_at(tile_x, tile_y)
                if tile_x is not None and tile_y is not None
                else None
            ),
            "tile_x": tile_x,
            "tile_y": tile_y,
            "is_online": True,
        }

    player_resident_ids = [item["id"] for item in data["nearby"]["players"]]
    controlled_ids: set[str] = set()
    if player_resident_ids:
        controlled_ids = set(
            (
                await db.execute(
                    select(AgentPlayer.resident_id).where(
                        AgentPlayer.resident_id.in_(player_resident_ids),
                        AgentPlayer.status == "active",
                    )
                )
            ).scalars().all()
        )
    nearby = {
        "residents": [
            spectator_actor(item, "npc") for item in data["nearby"]["residents"]
        ],
        "players": [
            spectator_actor(item, "agent" if item["id"] in controlled_ids else "human")
            for item in data["nearby"]["players"]
        ],
    }
    self_coords = visible_player_tiles.get(profile.user_id)
    self_location = get_location_id_at(*self_coords) if self_coords is not None else None
    self_view = {
        "slug": resident.slug,
        "name": resident.name,
        "kind": "agent",
        "status": profile.status,
        "district": self_location,
        "tile_x": self_coords[0] if self_coords is not None else None,
        "tile_y": self_coords[1] if self_coords is not None else None,
        "is_online": online,
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "agent": {
            "slug": resident.slug,
            "name": resident.name,
            "status": profile.status,
            "model_label": profile.model_label,
            "current_goal": current_goal,
            "is_online": online,
        },
        "self": self_view,
        "nearby": nearby,
        "location": _viewer_location(self_location),
        "recent_events": [],
        "observation_seq": data["observation_seq"],
    }


# F3: the public snapshot is identical for every anonymous caller and the
# endpoint is unauthenticated (120/min/IP only), so each request must not pay
# a fresh full projection. One process-local build is shared for a short TTL.
_SNAPSHOT_CACHE_TTL_SECONDS = 3.0
_snapshot_cache: dict[str, Any] = {"ts": -1e9, "data": None}


def _reset_snapshot_cache_for_tests() -> None:  # pragma: no cover - test hook
    _snapshot_cache["ts"] = -1e9
    _snapshot_cache["data"] = None


async def public_town_snapshot(db: AsyncSession) -> dict[str, Any]:
    cached = _snapshot_cache["data"]
    if (
        cached is not None
        and time.monotonic() - _snapshot_cache["ts"] < _SNAPSHOT_CACHE_TTL_SECONDS
    ):
        return cached
    data = await _build_public_town_snapshot(db)
    _snapshot_cache["ts"] = time.monotonic()
    _snapshot_cache["data"] = data
    return data


async def _build_public_town_snapshot(db: AsyncSession) -> dict[str, Any]:
    all_profiles = (await db.execute(select(AgentPlayer))).scalars().all()
    visible_player_tiles = await _visible_player_tiles_by_user_id()
    profiles: list[AgentPlayer] = []
    for profile in all_profiles:
        if profile.status != "active" or not profile.public_visible:
            continue
        user = await db.get(User, profile.user_id)
        if user is not None and not user.is_banned:
            profiles.append(profile)
    actors: list[dict[str, Any]] = []
    for profile in profiles:
        resident = await db.get(Resident, profile.resident_id)
        user = await db.get(User, profile.user_id)
        if resident is None or user is None or user.is_banned:
            continue
        coords = visible_player_tiles.get(profile.user_id)
        tx = coords[0] if coords is not None else None
        ty = coords[1] if coords is not None else None
        actors.append(
            {
                "slug": resident.slug,
                "name": resident.name,
                "kind": "agent",
                "status": profile.status,
                "district": (
                    get_location_id_at(tx, ty)
                    if tx is not None and ty is not None
                    else None
                ),
                "tile_x": tx,
                "tile_y": ty,
                "is_online": _agent_is_online(profile) and coords is not None,
                "model_label": profile.model_label,
            }
        )

    npcs = (
        await db.execute(select(Resident).where(Resident.resident_type != "player"))
    ).scalars().all()
    for resident in npcs:
        actors.append(
            {
                "slug": resident.slug,
                "name": resident.name,
                "kind": "npc",
                "status": resident.status,
                "district": get_location_id_at(resident.tile_x, resident.tile_y)
                or resident.district,
                "tile_x": resident.tile_x,
                "tile_y": resident.tile_y,
                "is_online": True,
            }
        )
    # Hidden or inactive Agent avatars must not be misclassified as humans in
    # public counts. They are omitted entirely from this projection.
    controlled_resident_ids = {profile.resident_id for profile in all_profiles}
    humans = (
        await db.execute(
            select(Resident).where(
                Resident.resident_type == "player",
                Resident.id.not_in(controlled_resident_ids),
            )
        )
    ).scalars().all()
    # Anonymous town projection deliberately excludes human identity and exact
    # coordinates. Counts remain useful without becoming a tracking surface.
    human_user_ids = [resident.creator_id for resident in humans if resident.creator_id]
    # F3: one HKEYS round-trip instead of one HEXISTS per human. Semantics are
    # byte-identical to the per-user is_online check (POSITIONS_KEY only).
    online_ids = await manager.online_user_ids()
    online_humans = sum(1 for user_id in human_user_ids if user_id in online_ids)
    online_agents = sum(1 for actor in actors if actor["kind"] == "agent" and actor["is_online"])
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "world_time": now_world().isoformat(),
        "counts": {
            "residents": len(actors) + len(humans),
            "agents": len(profiles),
            "humans": len(humans),
            "online": online_agents + online_humans + len(npcs),
        },
        "residents": actors,
        "activity": [],
        "privacy": {
            "human_players_included": False,
            "conversation_content_included": False,
        },
    }
