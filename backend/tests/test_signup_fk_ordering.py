"""Signup must INSERT users before transactions (FK enforced).

PostgreSQL enforces transactions.user_id -> users.id; without an explicit
flush between the two adds, SQLAlchemy's unit of work may emit the
Transaction INSERT first and registration 409s on every fresh PG database.
Regular fixtures run sqlite with FK enforcement off, so this file builds
its own engine with PRAGMA foreign_keys=ON to reproduce the PG behavior.
"""
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.user import User  # noqa: F401
from app.models.transaction import Transaction  # noqa: F401
import app.models.resident  # noqa: F401
import app.models.conversation  # noqa: F401
import app.models.memory  # noqa: F401
import app.models.personality_history  # noqa: F401
import app.models.system_config  # noqa: F401
import app.models.forge_session  # noqa: F401
import app.models.pending_message  # noqa: F401


@pytest.fixture
async def fk_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.anyio
async def test_register_user_inserts_user_before_bonus_transaction(fk_session):
    from app.services.auth_service import register_user

    user, token = await register_user(fk_session, "fk测试", "fk@test.dev", "pass1234")
    assert token
    assert user.soul_coin_balance == 100


@pytest.mark.anyio
async def test_linuxdo_signup_inserts_user_before_bonus_transaction(fk_session):
    from app.services.linuxdo_auth import LinuxDoUser, find_or_create_user

    ld = LinuxDoUser(id=777, username="fkuser", name="FK User",
                     active=True, trust_level=2, silenced=False)
    user, created = await find_or_create_user(fk_session, ld)
    assert created is True
    assert user.linuxdo_id == "777"
