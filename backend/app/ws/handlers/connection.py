"""WebSocket connection lifecycle: auth, session setup, dispatch loop, cleanup."""
import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.database import async_session
from app.models.resident import Resident
from app.models.user import User
from app.services.auth_service import verify_token
from app.services.player_chat_service import deliver_pending_messages
from app.ws.manager import manager
from app.ws.handlers.context import ConnectionContext
from app.ws.handlers import chat, movement, rating, player_chat

logger = logging.getLogger(__name__)

# Message type -> handler(ctx, data). Each handler owns its DB session scope.
MESSAGE_HANDLERS = {
    "cancel_queue": movement.handle_cancel_queue,
    "move": movement.handle_move,
    "start_chat": chat.handle_start_chat,
    "chat_msg": chat.handle_chat_msg,
    "end_chat": chat.handle_end_chat,
    "rate_chat": rating.handle_rate_chat,
    "player_chat": player_chat.handle_player_chat,
    "set_reply_mode": player_chat.handle_set_reply_mode,
}


# How long a fresh connection may take to send its auth message
AUTH_TIMEOUT_SECONDS = 10


async def _authenticate(ws: WebSocket) -> str | None:
    """Authenticate a freshly accepted connection.

    Preferred: first message `{"type": "auth", "token": "..."}` (P0-4c) —
    keeps tokens out of nginx/CF access logs. A `?token=` query param is
    still honored as a deprecated fallback for older clients.
    """
    token = ws.query_params.get("token", "")
    if token:
        logger.warning("WS auth via query string is deprecated; use an auth message")
        return verify_token(token)

    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
        data = json.loads(raw)
    except (TimeoutError, asyncio.TimeoutError, json.JSONDecodeError):
        return None
    if data.get("type") != "auth":
        return None
    return verify_token(str(data.get("token", "")))


async def websocket_handler(ws: WebSocket):
    """Handle a single WebSocket connection lifecycle."""
    await ws.accept()

    try:
        user_id = await _authenticate(ws)
    except WebSocketDisconnect:
        return
    if not user_id:
        await ws.close(code=4001, reason="Unauthorized")
        return

    manager.register(user_id, ws)
    await manager.send(user_id, {"type": "auth_ok"})

    user_name, spawn_x, spawn_y, sprite_key = await _load_session_info(user_id)
    await _claim_daily_reward(user_id)

    # Initialize position so other players can see us immediately
    await manager.update_position(user_id, spawn_x, spawn_y, "down", user_name)

    # Send spawn position to the connecting user so the frontend can place the player correctly
    await manager.send(user_id, {"type": "spawn_position", "x": spawn_x, "y": spawn_y})

    # Send current online players and announce join
    online_players = await manager.get_online_players(exclude=user_id)
    if online_players:
        await manager.send(user_id, {"type": "online_players", "players": online_players})

    # Deliver any pending (offline-queued) messages
    async with async_session() as db:
        pending = await deliver_pending_messages(db, user_id)
    for pm in pending:
        await manager.send(user_id, pm)

    # Broadcast join with position and sprite so existing players can render the new player
    pos = await manager.get_position(user_id) or {}
    await manager.broadcast(
        {
            "type": "player_joined",
            "player_id": user_id,
            "name": user_name,
            "x": pos.get("x", 0),
            "y": pos.get("y", 0),
            "direction": pos.get("direction", "down"),
            "sprite_key": sprite_key,
        },
        exclude=user_id,
    )

    ctx = ConnectionContext(user_id=user_id, user_name=user_name)

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            handler = MESSAGE_HANDLERS.get(data.get("type"))
            if handler:
                await handler(ctx, data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WS handler error for user %s: %s: %s", user_id, type(e).__name__, e, exc_info=True)
    finally:
        await _cleanup(ctx)


async def _load_session_info(user_id: str) -> tuple[str, int, int, str]:
    """Fetch user name, spawn position, and sprite from DB for this session."""
    user_name = user_id  # fallback
    spawn_x = 76 * 32
    spawn_y = 50 * 32
    sprite_key = ""
    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user_row = result.scalar_one_or_none()
        if user_row:
            user_name = user_row.name
            # Use persisted position if user has a player resident
            if user_row.player_resident_id:
                spawn_x = user_row.last_x
                spawn_y = user_row.last_y
                # Fetch sprite_key from the player's Resident
                res_result = await db.execute(
                    select(Resident.sprite_key).where(Resident.id == user_row.player_resident_id)
                )
                sk = res_result.scalar_one_or_none()
                if sk:
                    sprite_key = sk
    return user_name, spawn_x, spawn_y, sprite_key


async def _claim_daily_reward(user_id: str) -> None:
    """Attempt daily login reward and notify the user if claimed."""
    from app.services.daily_reward_service import claim_daily_reward

    async with async_session() as db:
        reward_result = await claim_daily_reward(db, user_id)
    if reward_result["claimed"]:
        await manager.send(user_id, {
            "type": "daily_reward",
            "amount": reward_result["amount"],
            "new_balance": reward_result["new_balance"],
        })


async def _cleanup(ctx: ConnectionContext) -> None:
    """Always clean up on disconnect/error: release locks, persist position."""
    user_id = ctx.user_id

    if ctx.in_chat:
        await manager.unlock_resident(ctx.resident.id)
        try:
            async with async_session() as db:
                result = await db.execute(select(Resident).where(Resident.id == ctx.resident.id))
                r = result.scalar_one_or_none()
                if r and r.status == "chatting":
                    r.status = "popular" if r.heat >= 50 else "idle"
                    await db.commit()
                # Also broadcast status reset so other clients update visuals
                await manager.broadcast(
                    {"type": "resident_status", "resident_slug": r.slug if r else "", "status": r.status if r else "idle"},
                )
        except Exception:
            pass

    # Save current position
    pos = await manager.get_position(user_id)
    if pos:
        try:
            async with async_session() as db:
                result = await db.execute(select(User).where(User.id == user_id))
                u = result.scalar_one_or_none()
                if u and u.player_resident_id:
                    u.last_x = int(pos.get("x", u.last_x))
                    u.last_y = int(pos.get("y", u.last_y))
                    await db.commit()
        except Exception:
            pass

    try:
        await manager.broadcast({"type": "player_left", "player_id": user_id}, exclude=user_id)
    except Exception:
        pass
    await manager.disconnect(user_id)
