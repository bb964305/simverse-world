"""P1-2: process-wide shared httpx.AsyncClient."""
import pytest
from unittest.mock import AsyncMock, patch

from app.http import get_client, close_client


@pytest.mark.anyio
async def test_get_client_returns_shared_instance():
    try:
        a = get_client()
        b = get_client()
        assert a is b
        assert not a.is_closed
    finally:
        await close_client()


@pytest.mark.anyio
async def test_close_client_closes_and_get_client_recreates():
    a = get_client()
    await close_client()
    assert a.is_closed

    b = get_client()
    try:
        assert b is not a
        assert not b.is_closed
    finally:
        await close_client()


@pytest.mark.anyio
async def test_client_does_not_trust_proxy_env():
    """Egress must not silently pick up HTTP_PROXY etc. (consistent with url_guard)."""
    try:
        assert get_client().trust_env is False
    finally:
        await close_client()


@pytest.mark.anyio
async def test_lifespan_closes_shared_client_on_shutdown():
    from app.main import app, lifespan

    with patch("app.main.heat_cron_loop", new=AsyncMock()), \
         patch("app.main.embedding_backfill_loop", new=AsyncMock()), \
         patch("app.main.agent_loop") as mock_agent_loop:
        mock_agent_loop.run = AsyncMock()
        async with lifespan(app):
            client = get_client()
            assert not client.is_closed
        assert client.is_closed
