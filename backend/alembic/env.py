import asyncio
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

# Import models so alembic sees them
from app.database import Base
from app.models.user import User
from app.models.resident import Resident
from app.models.conversation import Conversation, Message
from app.models.transaction import Transaction
from app.models.llm_usage import LLMUsage
from app.models.world_event import WorldEvent
from app.models.notification import Notification
from app.models.achievement import Achievement, UserAchievement
from app.models.shop import Item, Purchase
from app.models.location_visit import LocationVisit
from app.models.digest import Digest
from app.models.daily_quest import DailyQuest
from app.models.commission import Commission
from app.models.resident_goal import ResidentGoal
from app.models.bulletin_post import BulletinPost
from app.models.time_capsule import TimeCapsule

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    from app.config import settings

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.database_url
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
