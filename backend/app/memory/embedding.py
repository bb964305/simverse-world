"""Embedding client (PLAN_P3 后续批次 A 重构).

Three provider modes, resolved from settings:

1. ``embedding_base_url`` set  -> OpenAI-compatible ``POST {base}/embeddings``
   (百炼 compatible-mode, one-api relays, OpenAI proper). ``dimensions`` is
   passed explicitly — this retires the old "qwen3-embedding returns 2560,
   we truncate to 1024" hack for providers that honor it (发现区旧账).
2. otherwise                   -> local Ollama ``POST {ollama}/api/embed``
   (unchanged legacy dev behavior).
3. ``embedding_enabled=false`` -> short-circuit to None, zero network calls.
   Deployments without any embedding endpoint (vm212 before this fix) set
   this instead of letting every memory write + hourly backfill spam
   connection errors.

Failure semantics are unchanged (P0-5): return None, never zero-vectors —
callers keep the DB column NULL so cosine retrieval is not poisoned.
Repeated connection failures log a rate-limited warning (once per
``_ERR_LOG_INTERVAL``) instead of one line per call.
"""

import logging
import time

from app.config import settings
from app.http import get_client

logger = logging.getLogger(__name__)

# Rate-limit repeated failure logs: a dead endpoint would otherwise emit one
# warning per memory write plus the hourly backfill sweep.
_ERR_LOG_INTERVAL = 300.0
_last_err_log: float | None = None


def _log_failure(msg: str, *args) -> None:
    global _last_err_log
    now = time.monotonic()
    if _last_err_log is None or now - _last_err_log >= _ERR_LOG_INTERVAL:
        _last_err_log = now
        logger.warning(msg + " (further embedding errors suppressed for %ds)",
                       *args, int(_ERR_LOG_INTERVAL))
    else:
        logger.debug(msg, *args)


def _fit(vec: list[float], dim: int) -> list[float]:
    """Truncate/pad to the pgvector column width — fallback for providers
    that ignore the explicit ``dimensions`` request parameter."""
    if len(vec) > dim:
        return vec[:dim]
    if len(vec) < dim:
        return vec + [0.0] * (dim - len(vec))
    return vec


def _dim() -> int:
    # embedding_dimensions wins when the OpenAI-compatible provider is active;
    # both default to the vector(1024) column width from migration 004.
    if settings.embedding_base_url:
        return settings.embedding_dimensions
    return settings.ollama_embed_dimensions


async def _embed_openai(texts: list[str]) -> list[list[float] | None]:
    base = settings.embedding_base_url.rstrip("/")
    headers = {}
    if settings.embedding_api_key:
        headers["Authorization"] = f"Bearer {settings.embedding_api_key}"
    resp = await get_client().post(
        f"{base}/embeddings",
        json={
            "model": settings.embedding_model,
            "input": texts,
            "dimensions": settings.embedding_dimensions,
            "encoding_format": "float",
        },
        headers=headers,
        timeout=60.0,
    )
    if resp.status_code != 200:
        _log_failure("OpenAI-compatible embedding failed: %s %s",
                     resp.status_code, resp.text[:200])
        return [None] * len(texts)
    dim = _dim()
    by_index: dict[int, list[float]] = {}
    for item in resp.json().get("data", []):
        by_index[item.get("index", len(by_index))] = item.get("embedding", [])
    return [
        _fit(by_index[i], dim) if by_index.get(i) else None
        for i in range(len(texts))
    ]


async def _embed_ollama(texts: list[str]) -> list[list[float] | None]:
    resp = await get_client().post(
        f"{settings.ollama_base_url}/api/embed",
        json={
            "model": settings.ollama_embed_model,
            "input": texts,
            "truncate": True,
            "options": {"num_ctx": 2048},
        },
        timeout=60.0,
    )
    if resp.status_code != 200:
        _log_failure("Ollama embedding failed: %s %s",
                     resp.status_code, resp.text[:200])
        return [None] * len(texts)
    dim = _dim()
    embeddings = resp.json().get("embeddings", [])
    result: list[list[float] | None] = [
        _fit(v, dim) if v else None for v in embeddings
    ]
    while len(result) < len(texts):
        result.append(None)
    return result


async def generate_embeddings_batch(texts: list[str]) -> list[list[float] | None]:
    """Embed multiple texts in one provider call.

    Failed items are None so callers keep the DB column NULL (P0-5).
    """
    if not texts:
        return []
    if not settings.embedding_enabled:
        return [None] * len(texts)
    try:
        if settings.embedding_base_url:
            return await _embed_openai(texts)
        return await _embed_ollama(texts)
    except Exception as e:
        _log_failure("Embedding error: %s", e)
        return [None] * len(texts)


async def generate_embedding(text: str) -> list[float] | None:
    """Embed a single text. Returns None on empty input, disabled, or failure."""
    if not text or not text.strip():
        return None
    result = await generate_embeddings_batch([text])
    return result[0] if result else None
