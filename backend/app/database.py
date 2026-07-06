from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

# Explicit pool sizing (P0-2): asyncpg defaults (pool_size=5, max_overflow=10)
# are exhausted by ~15 concurrent chat sessions. SQLite doesn't use QueuePool,
# so only apply these to real server databases.
_pool_kwargs = {}
if not settings.database_url.startswith("sqlite"):
    _pool_kwargs = dict(
        pool_size=20,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
    )

engine = create_async_engine(settings.database_url, echo=False, **_pool_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session
