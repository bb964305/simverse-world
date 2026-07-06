"""Tests for the SSRF URL guard (P0-4d)."""
import pytest

from app.config import settings
from app.services.url_guard import ensure_url_is_public, UnsafeURLError


@pytest.fixture
def production_mode(monkeypatch):
    """Force debug=False so the IP-range check is active."""
    monkeypatch.setattr(settings, "debug", False)


@pytest.mark.anyio
async def test_rejects_non_http_scheme():
    with pytest.raises(UnsafeURLError, match="scheme"):
        await ensure_url_is_public("file:///etc/passwd")


@pytest.mark.anyio
async def test_rejects_missing_host():
    with pytest.raises(UnsafeURLError, match="no host"):
        await ensure_url_is_public("http://")


@pytest.mark.anyio
async def test_rejects_loopback(production_mode):
    with pytest.raises(UnsafeURLError, match="non-public"):
        await ensure_url_is_public("http://127.0.0.1:8080/v1")


@pytest.mark.anyio
async def test_rejects_private_range(production_mode):
    with pytest.raises(UnsafeURLError, match="non-public"):
        await ensure_url_is_public("http://10.0.0.5/api")


@pytest.mark.anyio
async def test_rejects_cloud_metadata(production_mode):
    """The classic SSRF target: link-local cloud metadata endpoint."""
    with pytest.raises(UnsafeURLError, match="non-public"):
        await ensure_url_is_public("http://169.254.169.254/latest/meta-data/")


@pytest.mark.anyio
async def test_rejects_localhost_hostname(production_mode):
    with pytest.raises(UnsafeURLError):
        await ensure_url_is_public("http://localhost:11434")


@pytest.mark.anyio
async def test_allows_private_in_debug_mode(monkeypatch):
    monkeypatch.setattr(settings, "debug", True)
    # Local Ollama must keep working in dev
    await ensure_url_is_public("http://localhost:11434")


@pytest.mark.anyio
async def test_allows_public_ip(production_mode):
    # IP literal avoids DNS dependency in tests (1.1.1.1 is globally routable)
    await ensure_url_is_public("https://1.1.1.1/v1")
