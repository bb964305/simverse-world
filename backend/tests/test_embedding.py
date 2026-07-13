import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.memory.embedding import generate_embedding, generate_embeddings_batch


def _patch_client(mock_client):
    """Embedding calls go through the shared client (P1-2)."""
    return patch("app.memory.embedding.get_client", return_value=mock_client)


@pytest.mark.anyio
async def test_generate_embedding_returns_list():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"embeddings": [[0.1] * 1024]}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    with _patch_client(mock_client):
        result = await generate_embedding("Hello world")

    assert isinstance(result, list)
    assert len(result) == 1024
    mock_client.post.assert_called_once()


@pytest.mark.anyio
async def test_generate_embedding_empty_text_returns_none():
    result = await generate_embedding("")
    assert result is None

    result = await generate_embedding("   ")
    assert result is None


@pytest.mark.anyio
async def test_generate_embeddings_batch():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"embeddings": [[0.1] * 1024, [0.2] * 1024]}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    with _patch_client(mock_client):
        results = await generate_embeddings_batch(["text one", "text two"])

    assert len(results) == 2
    assert len(results[0]) == 1024


@pytest.mark.anyio
async def test_generate_embeddings_batch_error_returns_nones():
    """P0-5: batch failure must yield None entries, never zero-vectors."""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    with _patch_client(mock_client):
        results = await generate_embeddings_batch(["text one", "text two"])

    assert results == [None, None]


@pytest.mark.anyio
async def test_generate_embeddings_batch_exception_returns_nones():
    mock_client = AsyncMock()
    mock_client.post.side_effect = RuntimeError("connection refused")
    with _patch_client(mock_client):
        results = await generate_embeddings_batch(["a", "b", "c"])

    assert results == [None, None, None]


@pytest.mark.anyio
async def test_generate_embeddings_batch_pads_missing_with_none():
    """Partial responses are padded with None, not zero-vectors."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"embeddings": [[0.1] * 1024]}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    with _patch_client(mock_client):
        results = await generate_embeddings_batch(["a", "b"])

    assert len(results) == 2
    assert len(results[0]) == 1024
    assert results[1] is None


@pytest.mark.anyio
async def test_generate_embedding_ollama_error_returns_none():
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    with _patch_client(mock_client):
        result = await generate_embedding("test")

    assert result is None


# --- PLAN_P3 后续批次 A: OpenAI-compatible provider + disable switch ---

@pytest.mark.anyio
async def test_openai_compatible_path_sends_dimensions_and_parses():
    """embedding_base_url set -> POST {base}/embeddings with explicit
    dimensions; response parsed by index (out-of-order safe)."""
    from app.config import settings
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": [
        {"index": 1, "embedding": [0.2] * 1024},
        {"index": 0, "embedding": [0.1] * 1024},
    ]}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    with _patch_client(mock_client), \
         patch.object(settings, "embedding_base_url", "http://relay/v1"), \
         patch.object(settings, "embedding_api_key", "sk-x"), \
         patch.object(settings, "embedding_model", "text-embedding-v4"):
        results = await generate_embeddings_batch(["a", "b"])

    assert results[0][0] == 0.1 and results[1][0] == 0.2
    url = mock_client.post.call_args[0][0]
    kwargs = mock_client.post.call_args[1]
    assert url == "http://relay/v1/embeddings"
    assert kwargs["json"]["dimensions"] == 1024
    assert kwargs["json"]["model"] == "text-embedding-v4"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-x"


@pytest.mark.anyio
async def test_openai_path_missing_item_is_none():
    from app.config import settings
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": [{"index": 0, "embedding": [0.1] * 1024}]}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    with _patch_client(mock_client), \
         patch.object(settings, "embedding_base_url", "http://relay/v1"):
        results = await generate_embeddings_batch(["a", "b"])
    assert results[0] is not None and results[1] is None


@pytest.mark.anyio
async def test_embedding_disabled_short_circuits_without_network():
    from app.config import settings
    mock_client = AsyncMock()
    with _patch_client(mock_client), \
         patch.object(settings, "embedding_enabled", False):
        single = await generate_embedding("hello")
        batch = await generate_embeddings_batch(["a", "b"])
    assert single is None
    assert batch == [None, None]
    mock_client.post.assert_not_called()


@pytest.mark.anyio
async def test_backfill_loop_exits_when_disabled():
    from app.config import settings
    from app.tasks.embedding_backfill import embedding_backfill_loop
    with patch.object(settings, "embedding_enabled", False):
        # Returns instead of looping forever — a plain await must finish.
        await embedding_backfill_loop()
