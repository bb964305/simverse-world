"""Seed the first shop items (D2). Run: python -m seed.shop_items (idempotent)."""
import asyncio

from app.database import async_session
from app.services.shop_service import seed_items


async def main() -> None:
    async with async_session() as db:
        n = await seed_items(db)
    print(f"Seeded/verified {n} shop items.")


if __name__ == "__main__":
    asyncio.run(main())
