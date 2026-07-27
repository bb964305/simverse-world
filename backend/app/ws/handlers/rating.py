"""rate_chat handler: save a conversation rating and update resident scores."""
import logging

from sqlalchemy import select, func

from app.config import settings
from app.database import async_session
from app.models.resident import Resident
from app.models.conversation import Conversation
from app.services.system_users import NON_USER_CREATOR_IDS
from app.ws.manager import manager
from app.ws.handlers.context import ConnectionContext

logger = logging.getLogger(__name__)


async def handle_rate_chat(ctx: ConnectionContext, data: dict) -> None:
    conv_id = data.get("conversation_id", "")
    rating_value = int(data.get("rating", 0))

    if not (1 <= rating_value <= 5):
        await manager.send(ctx.user_id, {"type": "error", "message": "Rating must be 1-5"})
        return

    async with async_session() as db:
        conv_result = await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == ctx.user_id,
            )
        )
        conv = conv_result.scalar_one_or_none()
        if not conv:
            await manager.send(ctx.user_id, {"type": "error", "message": "Conversation not found"})
            return

        conv.rating = rating_value
        await db.commit()

        # Realism P2-2: the rating carries player sentiment for the player↔resident
        # path (familiarity rides handle_end_chat). affinity ±0.03: 4-5★ positive,
        # 1-2★ negative, 3★ neutral. Reuses the rating event; no-op when gated off.
        if settings.realism_relations_enabled and conv.resident_id:
            d_aff = 0.0
            if rating_value >= 4:
                d_aff = settings.realism_rel_affinity_chat
            elif rating_value <= 2:
                d_aff = -settings.realism_rel_affinity_chat
            if d_aff:
                try:
                    from app.services import relation_service
                    await relation_service.bump(
                        db, conv.resident_id, ctx.user_id, d_affinity=d_aff,
                        type1="resident", type2="player",
                    )
                except Exception:
                    logger.warning("rating relation bump failed", exc_info=True)

        # Recalculate resident avg_rating
        avg_result = await db.execute(
            select(func.avg(Conversation.rating)).where(
                Conversation.resident_id == conv.resident_id,
                Conversation.rating.is_not(None),
            )
        )
        avg = avg_result.scalar()
        if avg is not None:
            res_result = await db.execute(
                select(Resident).where(Resident.id == conv.resident_id)
            )
            resident = res_result.scalar_one_or_none()
            if resident:
                resident.avg_rating = round(float(avg), 2)
                from app.services.scoring_service import compute_star_rating
                resident.star_rating = compute_star_rating(resident)
                await db.commit()
                # E1: a good/bad rating nudges the resident's mood.
                if rating_value >= 4:
                    from app.services.mood_service import apply_mood_event
                    await apply_mood_event(db, resident, dv=0.15)
                elif rating_value <= 2:
                    from app.services.mood_service import apply_mood_event
                    await apply_mood_event(db, resident, dv=-0.15)

        # Reward creator 5 SC for 4+ star rating
        if rating_value >= 4 and conv.resident_id:
            res_result = await db.execute(
                select(Resident).where(Resident.id == conv.resident_id)
            )
            resident = res_result.scalar_one_or_none()
            if resident and resident.creator_id and resident.creator_id not in NON_USER_CREATOR_IDS:
                from app.services.coin_service import reward
                await reward(db, resident.creator_id, 5, f"good_rating:{resident.slug}")

    await manager.send(ctx.user_id, {
        "type": "rating_saved",
        "conversation_id": conv_id,
        "rating": rating_value,
    })
