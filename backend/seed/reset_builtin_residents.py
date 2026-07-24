"""Reset the town's built-in cast: remove the old preset/demo residents and
seed the new 10-person original town cast (with relations + goals).

Run: python -m seed.reset_builtin_residents

What it deletes (player characters are NEVER touched):
- residents matching either
    a) one of the known legacy built-in slugs (old 14 presets + 5 demo NPCs), or
    b) creator_id == SYSTEM_USER_ID and resident_type == "npc" and slug not in
       the new roster (catches renamed/orphaned system NPCs)
- and, for each deleted resident, its dependent rows: messages/conversations,
  memories (own + about-them), personality history, goals, two-axis relations,
  follows/feed events, debates, treasury rows, llm_usage rows, bulletin posts
  and commissions they authored; users.player_resident_id pointers are nulled.

Everything runs in one session against DATABASE_URL, so the same script works
on the sqlite dev database and the production Postgres.
"""
import asyncio

from sqlalchemy import delete, or_, select, update

from app.database import async_session
from app.models.bulletin_post import BulletinPost
from app.models.commission import Commission
from app.models.conversation import Conversation, Message
from app.models.debate import Debate
from app.models.feed import FeedEvent, Follow
from app.models.llm_usage import LLMUsage
from app.models.memory import Memory
from app.models.personality_history import PersonalityHistory
from app.models.resident import Resident
from app.models.resident_goal import ResidentGoal
from app.models.resident_relation import ResidentRelation
from app.models.resident_treasury import ResidentTreasury
from app.models.user import User
from seed.preset_characters import PRESET_CHARACTERS, SYSTEM_USER_ID, seed_presets
from seed.seed_residents import ensure_system_user

# Old built-in roster: 14 legacy presets + 5 demo NPCs.
LEGACY_BUILTIN_SLUGS = {
    # legacy presets (nuwa-skill celebrities + 萧炎)
    "xiao-yan", "steve-jobs", "elon-musk", "charlie-munger", "feynman",
    "naval", "taleb", "paul-graham", "zhang-yiming", "karpathy",
    "ilya-sutskever", "mrbeast", "trump", "zhang-xuefeng",
    # legacy demo residents
    "isabella", "klaus", "adam", "mei", "tamara",
}

NEW_ROSTER_SLUGS = {c["slug"] for c in PRESET_CHARACTERS}


async def find_targets(db) -> list[Resident]:
    """Old built-in NPCs to remove. Player residents are always excluded."""
    result = await db.execute(
        select(Resident).where(
            Resident.resident_type != "player",
            or_(
                Resident.slug.in_(LEGACY_BUILTIN_SLUGS),
                Resident.creator_id == SYSTEM_USER_ID,
            ),
            Resident.slug.notin_(NEW_ROSTER_SLUGS),
        )
    )
    return list(result.scalars().all())


async def purge_residents(db, targets: list[Resident]) -> None:
    ids = [r.id for r in targets]
    slugs = [r.slug for r in targets]
    if not ids:
        return

    # Messages hang off conversations — delete them first.
    conv_ids = [
        row[0]
        for row in (
            await db.execute(select(Conversation.id).where(Conversation.resident_id.in_(ids)))
        ).all()
    ]
    if conv_ids:
        await db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        await db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))

    await db.execute(delete(Memory).where(
        or_(Memory.resident_id.in_(ids), Memory.related_resident_id.in_(ids))
    ))
    await db.execute(delete(PersonalityHistory).where(PersonalityHistory.resident_id.in_(ids)))
    await db.execute(delete(ResidentGoal).where(ResidentGoal.resident_id.in_(ids)))
    await db.execute(delete(ResidentRelation).where(
        or_(ResidentRelation.party_a.in_(ids), ResidentRelation.party_b.in_(ids))
    ))
    await db.execute(delete(LLMUsage).where(LLMUsage.resident_id.in_(ids)))
    await db.execute(delete(BulletinPost).where(BulletinPost.author_resident_id.in_(ids)))
    await db.execute(delete(Commission).where(Commission.issuer_resident_id.in_(ids)))
    await db.execute(delete(Follow).where(Follow.resident_slug.in_(slugs)))
    await db.execute(delete(FeedEvent).where(FeedEvent.resident_slug.in_(slugs)))
    await db.execute(delete(Debate).where(
        or_(Debate.resident_a_slug.in_(slugs), Debate.resident_b_slug.in_(slugs))
    ))
    await db.execute(delete(ResidentTreasury).where(ResidentTreasury.resident_slug.in_(slugs)))
    # A user "playing as" a deleted NPC would dangle — null the pointer.
    await db.execute(
        update(User)
        .where(User.player_resident_id.in_(ids))
        .values(player_resident_id=None)
        .execution_options(synchronize_session=False)
    )
    await db.execute(delete(Resident).where(Resident.id.in_(ids)))
    await db.commit()


async def main() -> None:
    async with async_session() as db:
        targets = await find_targets(db)
        if targets:
            print("Removing old built-in residents:")
            for r in targets:
                print(f"  - {r.slug} ({r.name})")
            await purge_residents(db, targets)
        else:
            print("No old built-in residents found.")

        await ensure_system_user(db)
        created = await seed_presets(db)

        kept = (await db.execute(
            select(Resident.slug, Resident.name, Resident.resident_type, Resident.district)
        )).all()
        print(f"\nSeeded {created} new residents. World now contains:")
        for slug, name, rtype, district in kept:
            print(f"  - [{rtype}] {name} ({slug}) @ {district}")


if __name__ == "__main__":
    asyncio.run(main())
