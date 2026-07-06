"""Player-to-player chat handlers: player_chat, set_reply_mode.

P0-2: the routing decision (DB reads + charge) runs in a short session that
is closed before any LLM auto-reply generation.
"""
from sqlalchemy import select

from app.database import async_session
from app.models.resident import Resident
from app.models.user import User
from app.services.player_chat_service import PlayerChatService, generate_auto_reply
from app.ws.manager import manager
from app.ws.handlers.context import ConnectionContext


async def handle_player_chat(ctx: ConnectionContext, data: dict) -> None:
    user_id = ctx.user_id
    target_id = data.get("target_id", "")
    text = data.get("text", "").strip()
    if not target_id or not text:
        await manager.send(user_id, {"type": "error", "message": "target_id and text required"})
        return

    target_online = target_id in manager.active

    # Short session: routing decision + charge only. No LLM inside.
    async with async_session() as db:
        svc = PlayerChatService(db)
        result = await svc.prepare_route(user_id, target_id, text, target_online)

    action = result.get("action")

    if action == "error":
        await manager.send(user_id, {"type": "error", "message": result["message"]})
    elif action == "forward":
        # Manual mode, target online -> forward to target
        payload = {
            "type": "player_chat_msg",
            "from_id": user_id,
            "text": text,
            "is_auto": False,
        }
        await manager.send(target_id, payload)
        await manager.send(user_id, {
            "type": "player_chat_sent",
            "target_id": target_id,
            "text": text,
        })
    elif action == "queued":
        await manager.send(user_id, {
            "type": "player_chat_queued",
            "target_id": target_id,
            "text": text,
        })
    elif action == "auto_reply":
        # LLM call happens with no DB session held
        reply_text = await generate_auto_reply(result["resident"], text)
        # Send auto-reply back to the sender
        await manager.send(user_id, {
            "type": "player_chat_reply",
            "from_id": target_id,
            "text": reply_text,
            "is_auto": True,
        })
        # Also notify the target if they are online
        if target_online:
            await manager.send(target_id, {
                "type": "player_chat_auto_sent",
                "from_id": user_id,
                "reply_text": reply_text,
                "original_text": text,
                "is_auto": True,
            })


async def handle_set_reply_mode(ctx: ConnectionContext, data: dict) -> None:
    user_id = ctx.user_id
    mode = data.get("mode", "")
    if mode not in ("auto", "manual"):
        await manager.send(user_id, {"type": "error", "message": "mode must be 'auto' or 'manual'"})
        return

    async with async_session() as db:
        # Fetch the user's player Resident and update reply_mode
        user_result = await db.execute(select(User).where(User.id == user_id))
        u = user_result.scalar_one_or_none()
        if not u or not u.player_resident_id:
            await manager.send(user_id, {"type": "error", "message": "No player resident bound"})
            return

        res_result = await db.execute(
            select(Resident).where(Resident.id == u.player_resident_id)
        )
        resident = res_result.scalar_one_or_none()
        if not resident:
            await manager.send(user_id, {"type": "error", "message": "Player resident not found"})
            return

        resident.reply_mode = mode
        await db.commit()

    await manager.send(user_id, {
        "type": "reply_mode_updated",
        "mode": mode,
    })
