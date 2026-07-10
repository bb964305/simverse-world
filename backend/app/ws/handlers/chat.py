"""NPC chat handlers: start_chat, chat_msg, end_chat.

Transaction boundaries (P0-2): a DB session is never held across an LLM
call. `handle_chat_msg` uses one short session for reads + charging + the
user message, closes it, streams the LLM reply with no connection held,
then opens a second short session to persist the assistant message.
"""
import asyncio
import logging
from datetime import datetime, UTC

from sqlalchemy import select

from app.database import async_session
from app.models.resident import Resident
from app.models.user import User
from app.models.conversation import Conversation, Message
from app.services.coin_service import charge, get_balance, reward_creator_passive
from app.llm.prompt import assemble_system_prompt
from app.llm.metering import Meter
from app.llm.budget import user_over_budget
from app.memory.service import MemoryService
from app.media.model_router import ModelRouter
from app.ws.manager import manager
from app.ws.protocol import StartChat, ChatMsg, EndChat
from app.ws.handlers.context import ConnectionContext
from app.ws.rate_limiter import ws_limiter
from app.config import settings

logger = logging.getLogger(__name__)

# E-08: max chat turns sent to the model per player-NPC message. Bounds the
# O(n²) input growth of long sessions; the full history is still persisted.
CHAT_HISTORY_WINDOW = 10


async def handle_start_chat(ctx: ConnectionContext, data: dict) -> None:
    try:
        msg = StartChat(**data)
    except Exception:
        await manager.send(ctx.user_id, {"type": "error", "message": "Invalid message format"})
        return
    slug = msg.resident_slug
    ctx.encounter_context = msg.context  # B2: scene context for this chat, if any

    async with async_session() as db:
        result = await db.execute(select(Resident).where(Resident.slug == slug))
        resident = result.scalar_one_or_none()
        if not resident:
            await manager.send(ctx.user_id, {"type": "error", "message": "Resident not found"})
            return
        # Queue if NPC is chatting or locked by another player
        if resident.status == "chatting" or (not await manager.lock_resident(resident.id, ctx.user_id)):
            pos = await manager.enqueue(resident.id, ctx.user_id)
            await manager.send(ctx.user_id, {
                "type": "chat_queued",
                "resident_slug": slug,
                "resident_name": resident.name,
                "position": pos,
            })
            return

        # Wake sleeping NPC — costs 3x token_cost_per_turn
        if resident.status == "sleeping":
            if not msg.wake:
                wake_cost = resident.token_cost_per_turn * 3
                await manager.send(ctx.user_id, {
                    "type": "wake_required",
                    "resident_slug": slug,
                    "resident_name": resident.name,
                    "cost": wake_cost,
                })
                await manager.unlock_resident(resident.id)
                return
            wake_cost = resident.token_cost_per_turn * 3
            ok = await charge(db, ctx.user_id, wake_cost, f"wake:{slug}")
            if not ok:
                await manager.send(ctx.user_id, {"type": "error", "message": "Insufficient Soul Coins"})
                await manager.unlock_resident(resident.id)
                return
            balance = await get_balance(db, ctx.user_id)
            await manager.send(ctx.user_id, {
                "type": "coin_update",
                "balance": balance,
                "delta": -wake_cost,
                "reason": f"wake:{slug}",
            })
            # Keep NPC awake: bump heat and update last_conversation_at
            # so heat_cron won't put them back to sleep for at least 7 days
            resident.heat = max(resident.heat, 10)
            resident.last_conversation_at = datetime.now(UTC)
            # Broadcast wake-up to all players (including self)
            await manager.broadcast(
                {"type": "resident_status", "resident_slug": slug, "status": "chatting",
                 "mood_label": resident.mood_label},
            )

        conv = Conversation(user_id=ctx.user_id, resident_id=resident.id)
        db.add(conv)
        resident.status = "chatting"
        await db.commit()
        await db.refresh(conv)

        ctx.conversation_id = conv.id
        ctx.resident = resident  # detached snapshot after session closes
        ctx.chat_messages = []

        # Retrieve memory context for this resident+user pair
        memory_svc = MemoryService(db)
        ctx.memory_context = await memory_svc.retrieve_context(
            resident_id=resident.id,
            user_id=ctx.user_id,
        )

    await manager.send(ctx.user_id, {"type": "chat_started", "resident_slug": slug})
    await manager.broadcast(
        {"type": "resident_status", "resident_slug": slug, "status": "chatting",
         "mood_label": ctx.resident.mood_label},
        exclude=ctx.user_id,
    )


async def handle_chat_msg(ctx: ConnectionContext, data: dict) -> None:
    if not ctx.in_chat:
        return
    # Rate limit: reject before any DB charge or LLM cost (P1-1 limit).
    # Per-user 60s sliding window shared across workers via Redis (P0-3b).
    if not await ws_limiter.check(ctx.user_id):
        await manager.send(ctx.user_id, {
            "type": "rate_limited",
            "message": "请求过快，请稍后再试",
            "limit_per_minute": settings.ws_rate_limit_per_minute,
        })
        return
    try:
        ChatMsg(**data)
    except Exception:
        await manager.send(ctx.user_id, {"type": "error", "message": "Invalid message format"})
        return

    text = data.get("text", "").strip()
    cost = ctx.resident.token_cost_per_turn

    # --- Session 1: validate, charge, persist the user message, read balance ---
    async with async_session() as db:
        conv_result = await db.execute(
            select(Conversation).where(Conversation.id == ctx.conversation_id)
        )
        fresh_conv = conv_result.scalar_one_or_none()
        if not fresh_conv:
            await manager.send(ctx.user_id, {"type": "error", "message": "Conversation not found"})
            return

        if not text:
            await manager.send(ctx.user_id, {"type": "error", "message": "Empty message"})
            return

        # Per-user daily budget cap (P1-1, E-24): don't charge or call the LLM
        # once the user has spent their day's allowance.
        if await user_over_budget(db, ctx.user_id):
            await manager.send(ctx.user_id, {
                "type": "budget_exceeded",
                "message": "今日对话额度已用完，明天再来吧",
            })
            return

        ok = await charge(db, ctx.user_id, cost, f"chat:{ctx.resident.slug}")
        if not ok:
            await manager.send(ctx.user_id, {"type": "error", "message": "Insufficient Soul Coins"})
            return

        db.add(Message(
            conversation_id=fresh_conv.id,
            role="user",
            content=text,
        ))
        fresh_conv.turns += 1
        await db.commit()

        result = await db.execute(
            select(User.soul_coin_balance).where(User.id == ctx.user_id)
        )
        balance = result.scalar_one()

        # S1: fetch active world events (60s-cached) while the session is open,
        # to fold into the dialogue system prompt below.
        from app.services.world_event_service import get_active_events_cached
        active_events = await get_active_events_cached(db)

        # A1: the resident's current life goal, so players can chat about it.
        active_goal_dict = None
        try:
            from app.services.goal_service import get_active_goal, serialize as _ser_goal
            _goal = await get_active_goal(db, ctx.resident.id)
            active_goal_dict = _ser_goal(_goal) if _goal else None
        except Exception:
            active_goal_dict = None

        # E2: last night's dream (24h), folded into the prompt.
        recent_dream = None
        try:
            from app.services.dream_service import get_recent_dream
            recent_dream = await get_recent_dream(db, ctx.resident.id)
        except Exception:
            recent_dream = None
    # Session closed — no DB connection held during LLM streaming below.

    await manager.send(ctx.user_id, {
        "type": "coin_update",
        "balance": balance,
        "delta": -cost,
        "reason": "chat",
    })

    media_url = data.get("media_url") or None
    media_type = data.get("media_type") or None

    ctx.chat_messages.append({"role": "user", "content": text})
    system_prompt = assemble_system_prompt(
        ctx.resident, memory_context=ctx.memory_context, world_events=active_events,
        life_goal=active_goal_dict, recent_dream=recent_dream,
    )
    # B2: if this chat started from an encounter, splice the scene in.
    if ctx.encounter_context:
        system_prompt += f"\n\n（当前场景：{ctx.encounter_context}）"

    # E-08: only send the last N turns to the model. ctx.chat_messages keeps the
    # full history (for persistence), but sending all of it re-pays for the whole
    # transcript every turn (O(n²)); a sliding window bounds long-session input.
    windowed_messages = ctx.chat_messages[-CHAT_HISTORY_WINDOW:]

    # --- LLM streaming (10-60s): runs without any DB session ---
    full_reply = ""
    try:
        model_router = ModelRouter()
        async for chunk in model_router.chat_with_media(
            system_prompt=system_prompt,
            messages=windowed_messages,
            media_url=media_url,
            media_type=media_type,
            meter=Meter(
                scenario="player_chat",
                resident_id=ctx.resident.id,
                user_id=ctx.user_id,
                conversation_id=ctx.conversation_id,
            ),
        ):
            full_reply += chunk
            await manager.send(ctx.user_id, {
                "type": "chat_reply",
                "text": chunk,
                "done": False,
            })
    except Exception as e:
        logger.error("LLM streaming error for %s: %s", ctx.resident.slug, e, exc_info=True)
        if not full_reply:
            full_reply = f"（对话出错了：{str(e)[:100]}）"
            await manager.send(ctx.user_id, {
                "type": "chat_reply",
                "text": full_reply,
                "done": False,
            })
    await manager.send(ctx.user_id, {"type": "chat_reply", "text": "", "done": True})

    ctx.chat_messages.append({"role": "assistant", "content": full_reply})

    # --- Session 2: persist the assistant message and side effects ---
    creator_notification = None
    async with async_session() as db:
        db.add(Message(
            conversation_id=ctx.conversation_id,
            role="assistant",
            content=full_reply,
        ))
        # Real per-attempt token accounting now lives in the llm_usage table,
        # metered inside the streaming call above (P1-1, E-03). The old
        # `tokens_used += len(full_reply)` char-count proxy covered only 3–9% of
        # the true cost and got worse as conversations grew, so it is retired.
        await db.commit()

        # If media was sent, store media_summary in event memory
        if media_url and media_type:
            # full_reply IS the media summary from the model's perspective
            memory_svc = MemoryService(db)
            await memory_svc.add_memory(
                resident_id=ctx.resident.id,
                type="event",
                content=f"玩家分享了一个{media_type}：{text or '(无文字描述)'}",
                importance=0.6,
                source="chat_player",
                related_user_id=ctx.user_id,
                media_url=media_url,
                media_summary=full_reply[:500],  # cap summary length
            )

        # Reward creator (1 SC per turn) and send notification if they're online
        creator_notification = await reward_creator_passive(
            db, ctx.resident.creator_id, ctx.resident.slug
        )

    if creator_notification:
        await manager.send(ctx.resident.creator_id, creator_notification)


async def handle_end_chat(ctx: ConnectionContext, data: dict) -> None:
    if not ctx.in_chat:
        return
    try:
        EndChat(**data)
    except Exception:
        await manager.send(ctx.user_id, {"type": "error", "message": "Invalid message format"})
        return

    resident_id = ctx.resident.id
    resident_slug = ctx.resident.slug

    async with async_session() as db:
        # Re-fetch in the current session to avoid detached-object mutation being dropped
        res_result = await db.execute(select(Resident).where(Resident.id == resident_id))
        fresh_resident = res_result.scalar_one_or_none()
        if fresh_resident:
            fresh_resident.status = "popular" if fresh_resident.heat >= 50 else "idle"
            fresh_resident.total_conversations += 1
            fresh_resident.last_conversation_at = datetime.now(UTC)

        conv_result = await db.execute(
            select(Conversation).where(Conversation.id == ctx.conversation_id)
        )
        fresh_conv = conv_result.scalar_one_or_none()
        if fresh_conv:
            fresh_conv.ended_at = datetime.now(UTC)

        await db.commit()

        prev_status = fresh_resident.status if fresh_resident else "idle"
        fresh_resident_name = fresh_resident.name if fresh_resident else ""
        fresh_resident_mood = fresh_resident.mood_label if fresh_resident else "calm"
        conv_id = fresh_conv.id if fresh_conv else ""
        conv_turns = fresh_conv.turns if fresh_conv else 0

    # S2: fire the chat_completed domain event (achievements, etc.). Handlers run
    # in their own sessions; a handler failure never affects the chat teardown.
    from app.events.bus import emit
    await emit(None, "chat_completed", user_id=ctx.user_id, resident_id=resident_id,
               turns=conv_turns, conversation_id=conv_id)

    # Release the resident lock
    await manager.unlock_resident(resident_id)

    await manager.send(ctx.user_id, {
        "type": "chat_ended",
        "conversation_id": conv_id,
    })

    # Notify next queued user
    next_user = await manager.dequeue(resident_id)
    if next_user:
        await manager.send(next_user, {
            "type": "queue_ready",
            "resident_slug": resident_slug,
            "resident_name": fresh_resident_name,
        })

    await manager.broadcast(
        {"type": "resident_status", "resident_slug": resident_slug, "status": prev_status,
         "mood_label": fresh_resident_mood},
        exclude=ctx.user_id,
    )

    saved_chat_messages = list(ctx.chat_messages)
    ctx.reset_chat()

    # Extract memories from the conversation (non-blocking)
    asyncio.create_task(extract_chat_memories(
        resident_id=resident_id,
        user_id=ctx.user_id,
        user_name=ctx.user_name,
        chat_messages=saved_chat_messages,
    ))


async def extract_chat_memories(
    resident_id: str,
    user_id: str,
    user_name: str,
    chat_messages: list[dict],
):
    """Background task: extract event memories and update relationship after chat ends."""
    if len(chat_messages) < 2:
        return  # Too short to extract meaningful memories

    try:
        async with async_session() as db:
            result = await db.execute(select(Resident).where(Resident.id == resident_id))
            resident = result.scalar_one_or_none()
            if not resident:
                return

            # Capture original SBTI type before memory extraction (which may trigger evolution)
            original_sbti_type = (resident.meta_json or {}).get("sbti", {}).get("type")

            # Format conversation text
            conv_text = "\n".join(
                f"{'玩家' if m['role'] == 'user' else resident.name}: {m['content']}"
                for m in chat_messages
            )

            svc = MemoryService(db)

            # 1. Extract event memories
            events = await svc.extract_events(
                resident=resident,
                other_name=user_name,
                conversation_text=conv_text,
                source="chat_player",
            )

            # 2. Update relationship memory
            if events:
                await svc.update_relationship_via_llm(
                    resident=resident,
                    other_name=user_name,
                    user_id=user_id,
                    event_summaries=[e.content for e in events],
                )

            # 3. Check if reflection is needed
            event_count = await svc.count_events_since_last_reflection(resident.id)
            if event_count >= 15:
                await svc.generate_reflections(resident=resident)

            # 4. Check for personality type change and broadcast
            await db.refresh(resident)
            new_sbti = (resident.meta_json or {}).get("sbti", {})
            new_type = new_sbti.get("type")
            old_type = original_sbti_type

            if new_type and old_type and new_type != old_type:
                await manager.broadcast(
                    {
                        "type": "resident_type_changed",
                        "resident_id": resident_id,
                        "old_type": old_type,
                        "new_type": new_type,
                        "type_name": new_sbti.get("type_name", ""),
                    },
                )

    except Exception as e:
        logger.warning("Memory extraction failed: %s", e)
