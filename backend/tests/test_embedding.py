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
