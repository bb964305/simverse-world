import os
import tempfile
from pathlib import Path

# Must be set before app.config is imported: tests run without a .env,
# and non-debug mode refuses the default JWT secret (P0-4b)
os.environ.setdefault("DEBUG", "true")

# 测试不依赖 backend/.env（burn-in 工程观察①）：裸环境（新 worktree/新机器）缺
# .env 时，config 默认值会把测试引向真实服务——DATABASE_URL 默认 postgres@localhost
# （test_rate_limit 的 ws chat 路径直连全局 engine，连接期 ConnectionRefused/SSL 崩），
# LLM key 为空串。这里在 app.config 导入前注入 dummy 配置补齐隔离；有 .env 的机器
# 走不到这个分支，行为不变。显式导出的环境变量（CI 的 DATABASE_URL）优先级更高，
# setdefault 不覆盖。
if not (Path(__file__).resolve().parent.parent / ".env").exists():
    os.environ.setdefault(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{tempfile.gettempdir()}/simverse_test_{os.getpid()}.db",
    )
    os.environ.setdefault("LLM_API_KEY", "test-dummy-key")

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.main import app
from app.database import Base, get_db

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session", autouse=True)
def _global_engine_tables():
    """Create tables on the GLOBAL app engine once per session (PLAN_P3 批次 0).

    A few handlers open sessions via ``app.database.async_session`` directly
    (e.g. ws chat in test_rate_limit), hitting the real DATABASE_URL — on a
    fresh file (CI uses /tmp/ci_dev.db) no migration has run, so the first
    such test dies with "no such table". Previously masked by pre-initialized
    local db files / full-suite ordering. Idempotent (create_all skips
    existing tables) and fail-open: a broken/read-only dev DB must not kill
    unrelated tests. The pool is disposed so no loop-bound connection leaks
    into per-test event loops.
    """
    import asyncio
    import warnings

    async def _create():
        from app.database import Base as _base, engine as _engine
        try:
            async with _engine.begin() as conn:
                await conn.run_sync(_base.metadata.create_all)
        finally:
            await _engine.dispose()

    try:
        asyncio.run(_create())
    except Exception as exc:  # pragma: no cover - fail-open by design
        warnings.warn(f"global-engine create_all skipped: {exc}")
    yield


@pytest.fixture(autouse=True)
def _fake_redis():
    """Install a fresh in-memory fakeredis for every test (P0-3b).

    The ConnectionManager online-state/locks/queues, the agent daily-action
    counter and the WS rate limiter all talk to Redis now; a per-test server
    gives each test a clean, isolated Redis without a running daemon.
    """
    import fakeredis.aioredis

    from app.redis_client import set_redis

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_redis(client)
    yield
    set_redis(None)


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Clear slowapi's in-memory storage before each test so REST rate-limit
    state never leaks across tests (P1-1 limit). The WS limiter now lives in
    Redis and is reset by the fresh `_fake_redis` server above."""
    from app.rate_limit import limiter as _rest_limiter
    # slowapi MemoryStorage.reset() drops all hit counters
    try:
        _rest_limiter._limiter.storage.reset()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _reset_public_snapshot_cache():
    """public_town_snapshot 的模块级 3s TTL 缓存(F3)每测清零,否则上一个测试的
    快照会被下一个测试直接吃到(仿 crowd_service._reset_for_tests 先例)。"""
    from app.services.agent_player_service import _reset_snapshot_cache_for_tests

    _reset_snapshot_cache_for_tests()
    yield


@pytest.fixture(autouse=True)
def _disable_llm_metering():
    """LLM usage metering (P1-1) writes through its own session/engine. Disable
    it by default so the broad suite never attempts a real-DB telemetry write;
    test_llm_usage re-enables it with an injected in-memory sqlite factory."""
    from app.config import settings as _s
    from app.llm import metering as _metering
    prev = _s.llm_metering_enabled
    _s.llm_metering_enabled = False
    yield
    _s.llm_metering_enabled = prev
    _metering.set_session_factory(None)


@pytest.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

@pytest.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

@pytest.fixture
async def client(db_engine):
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async def override_get_db():
        async with session_factory() as session:
            yield session
    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()

@pytest.fixture
def anyio_backend():
    return "asyncio"


def pytest_sessionfinish(session, exitstatus):
    """A required release run cannot become green by skipping prerequisites."""
    if os.environ.get("LAB_RELEASE_GATE", "").lower() not in {"1", "true", "yes", "on"}:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = len((reporter.stats if reporter is not None else {}).get("skipped", []))
    if skipped:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
