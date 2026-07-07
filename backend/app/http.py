"""Process-wide shared httpx.AsyncClient (P1-2).

Hot paths (embeddings, LLM probes, OAuth, health checks) reuse one
connection pool instead of paying a new TCP+TLS handshake per call.

trust_env=False: egress must not silently pick up proxy env vars,
consistent with the SSRF guard in services/url_guard. Callers pass
per-request timeouts; the client-level default stays httpx's 5s.
"""
import httpx

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Return the shared client, creating a fresh one if absent or closed."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(trust_env=False)
    return _client


async def close_client() -> None:
    """Close the shared client. Called from the app lifespan on shutdown."""
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        await client.aclose()
