from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.resident import Resident


async def list_residents(
    db: AsyncSession, limit: int | None = None, offset: int = 0
) -> list[Resident]:
    """List residents by heat desc. ``limit`` is opt-in (P1-3): callers that need
    the whole roster (e.g. the map) pass nothing; API clients can page with
    limit/offset. offset is applied only when meaningful."""
    stmt = select(Resident).order_by(Resident.heat.desc(), Resident.id)
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_resident_by_slug(db: AsyncSession, slug: str) -> Resident | None:
    result = await db.execute(select(Resident).where(Resident.slug == slug))
    return result.scalar_one_or_none()
