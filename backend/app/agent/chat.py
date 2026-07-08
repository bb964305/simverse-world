"""Inter-resident conversation engine with memory generation and broadcasting."""
import logging
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import CHAT_INITIATE_SYSTEM, CHAT_REPLY_SYSTEM
from app.config import settings
from app.llm.client import chat as llm_chat
from app.llm.metering import Meter
from app.memory.service import MemoryService
from app.models.resident import Resident

logger = logging.getLogger(__name__)

# Cooldown tracking: {frozenset(id1, id2): last_chat_timestamp}
_chat_cooldowns: dict[tuple[str, str], float] = {}


def _pair_key(a: Resident, b: Resident) -> tuple[str, str]:
    return tuple(sorted([a.id, b.id]))  # type: ignore[return-value]


def _is_on_cooldown(initiator: Resident, target: Resident) -> bool:
    key = _pair_key(initiator, target)
    last = _chat_cooldowns.get(key)
    if last is None:
        return False
    return (time.time() - last) < settings.agent_chat_cooldown


def _set_cooldown(initiator: Resident, target: Resident) -> None:
    _chat_cooldowns[_pair_key(initiator, target)] = time.time()


async def _get_relationship_text(svc: MemoryService, resident: Resident, other: Resident) -> str:
    rel = await svc.get_relationship(resident.id, resident_id_target=other.id)
    if rel:
        return rel.content
    return f"（首次和 {other.name} 交谈）"


def _build_chat_system(resident: Resident, other: Resident, rel_text: str, is_initiator: bool, history: str) -> str:
    sbti = (resident.meta_json or {}).get("sbti", {})
    sbti_type = sbti.get("type", "OJBK")
    sbti_name = sbti.get("type_name", "无所谓人")

    if is_initiator:
        return CHAT_INITIATE_SYSTEM.format(
            initiator_name=resident.name,
            sbti_type=sbti_type,
            sbti_name=sbti_name,
            target_name=other.name,
            persona_md=resident.persona_md or "",
            relationship_memory=rel_text,
        )
    else:
        # history is intentionally NOT injected here — it's supplied as the user
        # message; a {history} slot in the system prompt double-injected it (E-02).
        return CHAT_REPLY_SYSTEM.format(
            responder_name=resident.name,
            sbti_type=sbti_type,
            sbti_name=sbti_name,
            initiator_name=other.name,
            persona_md=resident.persona_md or "",
            relationship_memory=rel_text,
        )


async def resident_chat(
    db: AsyncSession,
    initiator: Resident,
    target: Resident,
    max_turns: int | None = None,
) -> dict[str, Any] | None:
    """Run a full inter-resident conversation.

    Flow:
    1. Pre-checks (cooldown, target availability)
    2. Lock both residents as 'socializing'
    3. Alternating LLM dialog for 3-8 turns
    4. Generate event memories for both (using MemoryService.extract_events)
    5. Update relationship memories for both
    6. Generate summary
    7. Unlock both residents
    8. Return summary dict

    Returns None if skipped (cooldown, busy target, etc.)
    """
    # Pre-checks
    if _is_on_cooldown(initiator, target):
        logger.debug("Chat skipped: %s<->%s on cooldown", initiator.slug, target.slug)
        return {"skipped": True, "reason": "cooldown"}

    if target.status in ("chatting", "socializing", "sleeping"):
        logger.debug("Chat skipped: %s is %s", target.slug, target.status)
        return {"skipped": True, "reason": "target_busy"}

    if max_turns is None:
        max_turns = settings.agent_chat_max_turns

    # Clamp turns to [3, 8]
    num_turns = max(3, min(max_turns, 8))

    # Lock both as socializing
    initiator.status = "socializing"
    target.status = "socializing"
    await db.commit()

    svc = MemoryService(db)

    # Fetch relationship memories for context
    init_rel_text = await _get_relationship_text(svc, initiator, target)
    tgt_rel_text = await _get_relationship_text(svc, target, initiator)

    dialog_lines: list[str] = []  # "Name: text"

    try:
        for turn in range(num_turns):
            is_initiator_turn = (turn % 2 == 0)
            speaker = initiator if is_initiator_turn else target
            listener = target if is_initiator_turn else initiator
            rel_text = init_rel_text if is_initiator_turn else tgt_rel_text

            history = "\n".join(dialog_lines[-6:])  # last 6 lines as context
            system_prompt = _build_chat_system(
                speaker, listener, rel_text,
                is_initiator=(turn == 0),
                history=history,
            )

            messages = [{"role": "user", "content": history or "开始对话"}]
            if turn > 0:
                # Append previous line as context
                messages = [{"role": "user", "content": history}]

            # E-17/E-26: 100 tok truncated ~1/4 of "50字" replies mid-sentence,
            # and the cut half-line then polluted subsequent turns' history.
            reply = (await llm_chat(
                system_prompt, messages, max_tokens=150,
                meter=Meter(scenario="chat_turn", resident_id=speaker.id),
            )).strip()[:200]
            dialog_lines.append(f"{speaker.name}: {reply}")

        dialog_text = "\n".join(dialog_lines)

        # Wrap-up: one merged LLM call does both residents' memory extraction +
        # relationship updates + the broadcast summary (E-04/E-05), replacing the
        # old five calls. It persists memories/relationships and runs evolution.
        summary_data = await svc.process_chat_wrapup(initiator, target, dialog_text)

        _set_cooldown(initiator, target)

        return {
            "initiator_slug": initiator.slug,
            "target_slug": target.slug,
            "summary": summary_data.get("summary", ""),
            "mood": summary_data.get("mood", "neutral"),
            "turns": len(dialog_lines),
        }

    except Exception as e:
        logger.warning("resident_chat failed %s<->%s: %s", initiator.slug, target.slug, e)
        return None

    finally:
        # Always unlock both residents
        initiator.status = "idle"
        target.status = "idle"
        try:
            await db.commit()
        except Exception:
            pass
