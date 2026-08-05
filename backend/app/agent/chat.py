"""Inter-resident conversation engine with memory generation and broadcasting."""
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import CHAT_INITIATE_SYSTEM, CHAT_REPLY_SYSTEM
from app.config import settings
from app.llm.client import chat as llm_chat
from app.llm.metering import Meter
from app.memory.service import MemoryService
from app.models.resident import Resident
from app.redis_client import get_redis
from app.services.mood_service import apply_mood_event, get_mood
from app.services.social_status_recovery import clear_socializing, mark_socializing

logger = logging.getLogger(__name__)

# Realism P0-5c: resident-pair chat cooldown lives in Redis (cross-worker,
# survives restart) instead of a process-local dict.

# Realism P0-3: map the wrap-up mood judgement to a (valence, arousal) nudge.
_CHAT_MOOD_DELTA = {
    "positive": ("realism_mood_positive_valence", "realism_mood_positive_arousal"),
    "negative": ("realism_mood_negative_valence", "realism_mood_negative_arousal"),
}


async def _apply_chat_mood(db, initiator, target, mood: str | None) -> None:
    """Write the resident-chat wrap-up mood back to both residents' mood_json
    (realism P0-3, main emotion-loop input source). Neutral / realism-off = no-op.
    Player-chat mood is intentionally not wired here (no such signal on that
    path; player sentiment flows via ratings — see PROGRESS deviation)."""
    if not settings.realism_enabled:
        return
    keys = _CHAT_MOOD_DELTA.get(mood or "")
    if keys is None:
        return
    dv = getattr(settings, keys[0])
    da = getattr(settings, keys[1])
    for res in (initiator, target):
        try:
            await apply_mood_event(db, res, dv, da)
        except Exception:
            logger.warning("chat mood write-back failed", exc_info=True)


async def _apply_chat_relations(db, a, b, mood: str | None) -> None:
    """Realism P2-2: a resident-resident conversation bumps the numeric relation.

    familiarity += 0.05 always (they spent time together); affinity ±0.03 by the
    wrap-up mood (positive/negative; neutral = no affinity change). Reuses the
    existing wrap-up mood judgement — zero new LLM. No-op when the relations gate
    is off, so the pre-P2 path is byte-for-byte unchanged."""
    if not settings.realism_relations_enabled:
        return
    d_aff = 0.0
    if mood == "positive":
        d_aff = settings.realism_rel_affinity_chat
    elif mood == "negative":
        d_aff = -settings.realism_rel_affinity_chat
    try:
        from app.services import relation_service
        await relation_service.bump(
            db, a.id, b.id,
            d_familiarity=settings.realism_rel_familiarity_chat,
            d_affinity=d_aff,
        )
    except Exception:
        logger.warning("chat relation bump failed", exc_info=True)


def _apply_chat_social(a, b) -> None:
    """Needs P1-10 恢复通路补全：一场成功的 resident 对话给双方
    social += realism_social_chat。此前 metabolize() 的每 tick 扣减是唯一
    写入方，social 锁死 0 后 decide 的聊天 nudge（social<0.4）永久激活。
    write_needs 负责 clamp [0,1]；持久化搭 resident_chat finally 块的
    commit（与 _apply_chat_relations 同一次落库）。"""
    if not settings.realism_enabled:
        return
    try:
        from app.agent.needs import get_needs, write_needs
        for res in (a, b):
            needs = get_needs(res)
            needs["social"] = needs["social"] + settings.realism_social_chat
            write_needs(res, needs)
    except Exception:
        logger.warning("chat social restore failed", exc_info=True)


async def _apply_contagion(db, a, b) -> None:
    """Realism P1-11: emotion contagion — after a conversation both residents'
    valence moves toward their shared mean (a bad day ripples out)."""
    if not settings.realism_enabled:
        return
    va = float(get_mood(a).get("valence", 0.0))
    vb = float(get_mood(b).get("valence", 0.0))
    mean = (va + vb) / 2.0
    rate = settings.realism_contagion_rate
    try:
        await apply_mood_event(db, a, rate * (mean - va), 0.0)
        await apply_mood_event(db, b, rate * (mean - vb), 0.0)
    except Exception:
        logger.warning("emotion contagion failed", exc_info=True)


async def _apply_duty_chat_effects(db, a, b) -> None:
    """Duty system: a host-type resident (咖啡馆老板娘) makes conversations
    restorative — the *other* party leaves with a small mood lift and a touch of
    extra goodwill. Independent of the realism gates (it is a duty feature, not
    a realism feature) and fail-open."""
    try:
        from app.services import relation_service
        from app.services.duty_service import perk as _duty_perk

        for host, guest in ((a, b), (b, a)):
            uplift = _duty_perk(host, "chat_mood_uplift", 0.0)
            if uplift > 0:
                await apply_mood_event(db, guest, uplift, 0.0)
            aff_bonus = _duty_perk(host, "chat_affinity_bonus", 0.0)
            if aff_bonus > 0:
                await relation_service.bump(db, host.id, guest.id, d_affinity=aff_bonus)
    except Exception:
        logger.warning("duty chat effects failed", exc_info=True)


def _pair_key(a: Resident, b: Resident) -> str:
    lo, hi = sorted([a.id, b.id])
    return f"sv:chat_cd:{lo}:{hi}"


async def _is_on_cooldown(initiator: Resident, target: Resident) -> bool:
    return bool(await get_redis().exists(_pair_key(initiator, target)))


async def _set_cooldown(initiator: Resident, target: Resident) -> None:
    await get_redis().set(_pair_key(initiator, target), "1", ex=settings.agent_chat_cooldown)


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
    if await _is_on_cooldown(initiator, target):
        logger.debug("Chat skipped: %s<->%s on cooldown", initiator.slug, target.slug)
        return {"skipped": True, "reason": "cooldown"}

    if target.status in ("chatting", "socializing", "sleeping"):
        logger.debug("Chat skipped: %s is %s", target.slug, target.status)
        return {"skipped": True, "reason": "target_busy"}

    if max_turns is None:
        max_turns = settings.agent_chat_max_turns

    # Clamp turns to [3, 8]
    num_turns = max(3, min(max_turns, 8))

    # P2-3: old friends linger, strangers keep it short — move the turn count with
    # familiarity within [3, 8] (≈3-4 for near-strangers, ≈6-8 for close ties).
    # Only when the caller didn't pin max_turns and the relations gate is on.
    if settings.realism_relations_enabled and max_turns == settings.agent_chat_max_turns:
        try:
            from app.services import relation_service
            rel_row = await relation_service.get_pair(db, initiator.id, target.id)
            fam = rel_row.familiarity if rel_row else 0.0
            num_turns = relation_service.turns_for_familiarity(fam)
        except Exception:
            logger.warning("familiarity turn interpolation failed", exc_info=True)

    # Lock both as socializing. R4: the lock is stamped with a timestamp so a
    # worker that dies before the `finally` below cannot leave the pair stuck
    # forever — social_status_recovery sweeps stamps older than SOCIAL_LOCK_TTL.
    mark_socializing(initiator, partner_id=target.id)
    mark_socializing(target, partner_id=initiator.id)
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

        # Realism P0-3: write the wrap-up mood back to both residents.
        await _apply_chat_mood(svc.db, initiator, target, summary_data.get("mood"))
        # Realism P1-11: emotion contagion toward the pair's mean valence.
        await _apply_contagion(svc.db, initiator, target)
        # Realism P2-2: numeric relation bump (familiarity + affinity by mood).
        await _apply_chat_relations(svc.db, initiator, target, summary_data.get("mood"))
        # Needs P1-10 恢复通路：对话是 social 的（唯一主）回补来源。
        _apply_chat_social(initiator, target)
        # Duty system: host-type residents leave the other party warmer.
        await _apply_duty_chat_effects(svc.db, initiator, target)

        # E3: each may pass a third-party rumor to the other (best-effort).
        try:
            from app.services.gossip_service import maybe_gossip
            await maybe_gossip(svc.db, initiator, target)
            await maybe_gossip(svc.db, target, initiator)
        except Exception:
            logger.warning("gossip handoff failed", exc_info=True)

        await _set_cooldown(initiator, target)

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
        # Always unlock both residents (and drop the R4 lock stamp)
        clear_socializing(initiator)
        clear_socializing(target)
        try:
            await db.commit()
        except Exception:
            pass
