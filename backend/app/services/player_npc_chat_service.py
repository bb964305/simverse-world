"""Shared building blocks for player-to-NPC dialogue transports."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.llm.metering import Meter
from app.llm.prompt import assemble_system_prompt
from app.media.model_router import ModelRouter
from app.memory.service import MemoryService
from app.models.agent_player import AgentNpcChatTurnReceipt, AgentPlayer
from app.models.resident import Resident
from app.ws.manager import manager


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SingleTurnPrompt:
    system_prompt: str
    messages: list[dict[str, str]]


def npc_chat_lock_owner(agent_player_id: str, lease_token: str) -> str:
    """Namespace a receipt lease so it cannot collide with a human user id."""
    return f"agent:{agent_player_id}:{lease_token}"


async def release_npc_chat_lock_and_notify(
    *,
    agent_player_id: str,
    lease_token: str | None,
    resident_id: str,
    resident_slug: str,
    resident_name: str,
) -> None:
    """Owner-safely release a lock and wake one existing WebSocket waiter."""
    try:
        if lease_token:
            await manager.unlock_resident(
                resident_id,
                expected_owner=npc_chat_lock_owner(agent_player_id, lease_token),
            )
        # A false owner-checked delete can mean either "already expired" or
        # "re-acquired by somebody else". Only hand off the queue when no new
        # owner exists.
        if await manager.resident_lock_owner(resident_id) is not None:
            return
        next_user = await manager.dequeue(resident_id)
        if next_user:
            await manager.send(
                next_user,
                {
                    "type": "queue_ready",
                    "resident_slug": resident_slug,
                    "resident_name": resident_name,
                },
            )
    except Exception:
        logger.warning("Agent NPC lock release/queue handoff failed", exc_info=True)


async def recover_expired_npc_chat_turns(db: AsyncSession) -> int:
    """Release durable/Redis leases left by crashed Agent chat requests.

    Receipts remain pending and can be resumed with the same turn id. Keeping
    recovery separate from failure avoids charging or duplicating a user
    message merely because an API worker died during the model call.
    """
    now = datetime.now(UTC)
    candidate_ids = (
        await db.execute(
            select(AgentNpcChatTurnReceipt.id)
            .where(
                AgentNpcChatTurnReceipt.status == "pending",
                AgentNpcChatTurnReceipt.lease_expires_at.is_not(None),
                AgentNpcChatTurnReceipt.lease_expires_at <= now,
            )
        )
    ).scalars().all()
    releases: list[tuple[str, str | None, str, str, str]] = []
    recovered = 0
    for receipt_id in candidate_ids:
        candidate = await db.get(AgentNpcChatTurnReceipt, receipt_id)
        if candidate is None:
            continue
        # Match the request path's global lock order: profile -> receipt -> NPC.
        profile = await db.get(AgentPlayer, candidate.agent_player_id)
        if profile is None:
            continue
        await db.refresh(profile, with_for_update=True)
        receipt = (
            await db.execute(
                select(AgentNpcChatTurnReceipt)
                .where(AgentNpcChatTurnReceipt.id == receipt_id)
                .with_for_update(skip_locked=True)
                # The candidate lookup may already have placed this receipt in
                # the identity map. Refresh under the lock so a concurrent
                # retry that renewed the lease cannot be cleaned up using
                # stale ORM attributes.
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if receipt is None or receipt.status != "pending":
            continue
        expires_at = receipt.lease_expires_at
        if expires_at is None:
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at > now:
            continue
        old_token = receipt.lease_token
        resident = await db.get(Resident, receipt.resident_id)
        cleanup_lock_acquired = False
        if resident is not None:
            await db.refresh(resident, with_for_update=True)
            if old_token:
                try:
                    # Re-acquire/renew the exact old owner atomically. If a
                    # human obtained the lock after expiry this returns False,
                    # and we must not overwrite that human's DB status.
                    cleanup_lock_acquired = await manager.lock_resident(
                        resident.id,
                        npc_chat_lock_owner(receipt.agent_player_id, old_token),
                        ttl_seconds=30,
                    )
                except Exception:
                    logger.warning(
                        "Expired Agent NPC lock ownership check failed",
                        exc_info=True,
                    )
                    continue
            if cleanup_lock_acquired and resident.status == "chatting":
                resident.status = "popular" if resident.heat >= 50 else "idle"
            if cleanup_lock_acquired:
                releases.append(
                    (
                        receipt.agent_player_id,
                        old_token,
                        resident.id,
                        resident.slug,
                        resident.name,
                    )
                )
        if profile.operation_token == old_token:
            profile.operation_kind = None
            profile.operation_token = None
            profile.operation_expires_at = None
        receipt.lease_token = None
        receipt.lease_expires_at = None
        receipt.updated_at = now
        recovered += 1
    if recovered:
        # Publish queue readiness only after the durable Resident status reset
        # is visible to the next start_chat request.
        await db.commit()
        for agent_player_id, token, resident_id, slug, name in releases:
            await release_npc_chat_lock_and_notify(
                agent_player_id=agent_player_id,
                lease_token=token,
                resident_id=resident_id,
                resident_slug=slug,
                resident_name=name,
            )
    return recovered


async def run_agent_npc_chat_reaper() -> None:
    """Continuously recover abandoned turns, including after worker restart."""
    while True:
        try:
            async with async_session() as db:
                await recover_expired_npc_chat_turns(db)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.warning("Agent NPC chat reaper failed", exc_info=True)
        await asyncio.sleep(15)


async def build_single_turn_prompt(
    db: AsyncSession,
    *,
    resident: Resident,
    user_id: str,
    text: str,
    context: str | None = None,
) -> SingleTurnPrompt:
    """Assemble the same public/memory context used by player NPC chat."""
    memory_context: Any = None
    try:
        memory_context = await MemoryService(db).retrieve_context(
            resident_id=resident.id,
            user_id=user_id,
            query_text=text[-500:],
            commit_access=False,
            allow_embedding=False,
        )
    except Exception:
        memory_context = None

    active_events: list[dict[str, Any]] = []
    try:
        from app.services.world_event_service import get_active_events_cached

        active_events = await get_active_events_cached(db)
    except Exception:
        active_events = []

    town_facts = None
    try:
        from app.services.town_facts_service import build_town_facts

        town_facts = await build_town_facts(db, resident)
    except Exception:
        town_facts = None

    active_goal = None
    try:
        from app.services.goal_service import get_active_goal, serialize

        goal = await get_active_goal(db, resident.id)
        active_goal = serialize(goal) if goal else None
    except Exception:
        active_goal = None

    recent_dream = None
    try:
        from app.services.dream_service import get_recent_dream

        recent_dream = await get_recent_dream(db, resident.id)
    except Exception:
        recent_dream = None

    system_prompt = assemble_system_prompt(
        resident,
        memory_context=memory_context,
        world_events=active_events,
        life_goal=active_goal,
        recent_dream=recent_dream,
        town_facts=town_facts,
    )
    if context:
        system_prompt += f"\n\n（当前场景：{context}）"
    return SingleTurnPrompt(
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": text}],
    )


async def generate_single_turn_reply(
    *,
    prompt: SingleTurnPrompt,
    resident_id: str,
    user_id: str,
    conversation_id: str,
) -> str:
    """Run one player-visible NPC response without holding a DB session."""
    reply = ""
    router = ModelRouter()
    async for chunk in router.chat_with_media(
        system_prompt=prompt.system_prompt,
        messages=prompt.messages,
        media_url=None,
        media_type=None,
        meter=Meter(
            scenario="player_chat",
            resident_id=resident_id,
            user_id=user_id,
            conversation_id=conversation_id,
        ),
    ):
        reply += chunk
    return reply.strip()


async def extract_player_chat_memories(
    *,
    resident_id: str,
    user_id: str,
    user_name: str,
    chat_messages: list[dict[str, str]],
) -> None:
    """Apply the same best-effort memory effects as a completed WebSocket chat."""
    if len(chat_messages) < 2:
        return
    try:
        async with async_session() as db:
            resident = await db.get(Resident, resident_id)
            if resident is None:
                return
            original_sbti_type = (resident.meta_json or {}).get("sbti", {}).get("type")
            conversation_text = "\n".join(
                f"{'玩家' if item['role'] == 'user' else resident.name}: {item['content']}"
                for item in chat_messages
            )
            memory = MemoryService(db)
            events = await memory.extract_events(
                resident=resident,
                other_name=user_name,
                conversation_text=conversation_text,
                source="chat_player",
            )
            if events:
                await memory.update_relationship_via_llm(
                    resident=resident,
                    other_name=user_name,
                    user_id=user_id,
                    event_summaries=[event.content for event in events],
                )
            if await memory.count_events_since_last_reflection(resident.id) >= 15:
                await memory.generate_reflections(resident=resident)
            await db.refresh(resident)
            sbti = (resident.meta_json or {}).get("sbti", {})
            new_type = sbti.get("type")
            if new_type and original_sbti_type and new_type != original_sbti_type:
                await manager.broadcast(
                    {
                        "type": "resident_type_changed",
                        "resident_id": resident_id,
                        "old_type": original_sbti_type,
                        "new_type": new_type,
                        "type_name": sbti.get("type_name", ""),
                    }
                )
    except Exception:
        logger.warning("Agent NPC chat memory extraction failed", exc_info=True)
