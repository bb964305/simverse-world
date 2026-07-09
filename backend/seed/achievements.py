"""Seed achievement definitions into the achievements table (S2).

Run with: python -m seed.achievements
Idempotent — upserts ACHIEVEMENT_DEFS (skips codes that already exist).
"""
import asyncio

from app.database import async_session
from app.events.achievements import seed_achievements


async def main() -> None:
    async with async_session() as db:
        n = await seed_achievements(db)
    print(f"Seeded/verified {n} achievement definitions.")


if __name__ == "__main__":
    asyncio.run(main())
