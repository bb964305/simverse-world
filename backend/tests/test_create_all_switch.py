"""P0-6: lifespan must not run create_all unless auto_create_tables is on."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.main import app, lifespan


def test_auto_create_tables_defaults_to_false():
    assert settings.model_fields["auto_create_tables"].default is False


def _fake_engine():
    engine = MagicMock()
    conn = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    engine.begin.return_value = cm
    return engine, conn


async def _forever():
    await asyncio.sleep(3600)


def _lifespan_patches(engine):
    fake_agent_loop = MagicMock()
    fake_agent_loop.run = _forever
    return [
        patch("app.database.engine", engine),
        patch("app.main.heat_cron_loop", _forever),
        patch("app.main.embedding_backfill_loop", _forever),
        patch("app.main.agent_loop", fake_agent_loop),
    ]


@pytest.mark.anyio
async def test_lifespan_skips_create_all_by_default():
    engine, _ = _fake_engine()
    patches = _lifespan_patches(engine)
    for p in patches:
        p.start()
    try:
        with patch.object(settings, "auto_create_tables", False):
            async with lifespan(app):
                pass
    finally:
        for p in patches:
            p.stop()

    engine.begin.assert_not_called()


@pytest.mark.anyio
async def test_lifespan_runs_create_all_when_enabled():
    engine, conn = _fake_engine()
    patches = _lifespan_patches(engine)
    for p in patches:
        p.start()
    try:
        with patch.object(settings, "auto_create_tables", True):
            async with lifespan(app):
                pass
    finally:
        for p in patches:
            p.stop()

    engine.begin.assert_called_once()
    conn.run_sync.assert_awaited_once()
