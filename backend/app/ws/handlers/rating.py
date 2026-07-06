"""rate_chat handler: save a conversation rating and update resident scores."""
from sqlalchemy import select, func

from app.database import async_session
from app.models.resident import Resident
from app.models.conversation import Conversation
from app.ws.manager import manager
from app.ws.handlers.context import ConnectionContext


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

        # Reward creator 5 SC for 4+ star rating
        if rating_value >= 4 and conv.resident_id:
            res_result = await db.execute(
                select(Resident).where(Resident.id == conv.resident_id)
            )
            resident = res_result.scalar_one_or_none()
            if resident:
                from app.services.coin_service import reward
                await reward(db, resident.creator_id, 5, f"good_rating:{resident.slug}")

    await manager.send(ctx.user_id, {
        "type": "rating_saved",
        "conversation_id": conv_id,
        "rating": rating_value,
    })
