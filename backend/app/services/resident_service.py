from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.resident import Resident


async def list_residents(
    db: AsyncSession, limit: int | None = None, offset: int = 0
) -> list[Resident]:
    """List residents by heat desc. ``limit`` is opt-in (P1-3): callers that need
    the whole roster (e.g. the map) pass nothing; API clients can page with
    limit/offset. offset is applied only when meaningful."""
    # This public roster backs the Phaser NPC layer. Player avatars (human or
    # external Agent) are delivered by presence/player events; returning them
    # here would render a duplicate static NPC and would expose a misleading
    # NPC-chat target.
    stmt = (
        select(Resident)
        .where(Resident.resident_type != "player")
        .order_by(Resident.heat.desc(), Resident.id)
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_resident_by_slug(db: AsyncSession, slug: str) -> Resident | None:
    result = await db.execute(select(Resident).where(Resident.slug == slug))
    return result.scalar_one_or_none()


async def resolve_resident_mentions(db: AsyncSession, names: list[str]) -> dict[str, str]:
    """Map mentioned names/slugs -> resident.id. Unknown / non-string entries are
    dropped (an LLM may emit ``mentioned_resident`` as a list; this runs outside the
    extract/wrapup try/except, so it must never raise — burn-in review finding)."""
    cleaned = [n.strip() for n in names if isinstance(n, str) and n.strip()]
    if not cleaned:
        return {}
    rows = (await db.execute(
        select(Resident).where(
            or_(Resident.name.in_(cleaned), Resident.slug.in_(cleaned))
        )
    )).scalars().all()
    mapping: dict[str, str] = {}
    for r in rows:
        mapping[r.name] = r.id
        mapping[r.slug] = r.id
    return {n: mapping[n] for n in cleaned if n in mapping}
