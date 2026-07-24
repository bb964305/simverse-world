"""Seed the town's built-in residents (the 10-person original cast).

Run: python -m seed.seed_residents

The cast, social relations and life goals all live in
``seed.preset_characters``; this module is the runnable entrypoint that
creates the system user and applies the seed. ``SEED_DATA`` is re-exported
for callers/tests that inspect the seed roster.
"""
import asyncio

from sqlalchemy import select

from app.database import engine, async_session, Base
from app.models.user import User
from seed.preset_characters import PRESET_CHARACTERS, SYSTEM_USER_ID, seed_presets

# Backwards-compatible alias: the built-in roster (canonical location ids).
SEED_DATA = PRESET_CHARACTERS


async def ensure_system_user(db) -> None:
    existing = await db.execute(select(User).where(User.id == SYSTEM_USER_ID))
    if not existing.scalar_one_or_none():
        db.add(User(
            id=SYSTEM_USER_ID,
            name="System",
            email="system@skills.world",
            soul_coin_balance=0,
        ))
        await db.flush()
        await db.commit()


async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as db:
        await ensure_system_user(db)
        count = await seed_presets(db)
    print(f"Seeded {count} new residents (roster size: {len(SEED_DATA)})")


if __name__ == "__main__":
    asyncio.run(seed())
