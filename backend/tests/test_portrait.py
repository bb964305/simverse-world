import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.portrait_service import (
    generate_portrait,
    build_portrait_prompt,
    save_portrait_image,
    PORTRAIT_DIR,
)


def test_build_portrait_prompt():
    """Should build a descriptive prompt from persona text."""
    persona = """# 人格档案
## Layer 0: 第一印象
- 外貌：短发，戴眼镜，穿白色实验服
- 气质：冷静、理性、略带神秘感
"""
    prompt = build_portrait_prompt("Dr. Nova", persona)
    assert "Dr. Nova" in prompt
    assert "Q-style" in prompt or "chibi" in prompt or "pixel" in prompt
    assert isinstance(prompt, str)
    assert len(prompt) > 20


def test_build_portrait_prompt_no_persona():
    """Should work even with empty persona."""
    prompt = build_portrait_prompt("Unknown", "")
    assert "Unknown" in prompt
    assert len(prompt) > 10


@pytest.mark.anyio
async def test_generate_portrait_success():
    """Should call Gemini API and save image."""
    import base64
    fake_image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # fake PNG header

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(fake_image_bytes).decode(),
                    }
                }]
            }
        }]
    }

    # generate_portrait now short-circuits to None when the portrait LLM is
    # unconfigured — inject a fake endpoint so the request path is exercised.
    from app.config import settings

    with patch.object(settings, "portrait_llm_base_url", "https://gemini.test/v1"), \
         patch.object(settings, "portrait_llm_api_key", "test-key"), \
         patch("app.services.portrait_service.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_get_client.return_value = mock_client

        with patch("app.services.portrait_service.save_portrait_image") as mock_save:
            mock_save.return_value = "/static/portraits/test-id.png"

            url = await generate_portrait("test-id", "Dr. Nova", "冷静理性的科学家")

    assert url == "/static/portraits/test-id.png"
    mock_client.post.assert_called_once()
    mock_save.assert_called_once()


@pytest.mark.anyio
async def test_generate_portrait_api_error():
    """Should return None on API failure (with the endpoint configured, so the
    500 branch is actually exercised rather than the unconfigured guard)."""
    from app.config import settings

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    with patch.object(settings, "portrait_llm_base_url", "https://gemini.test/v1"), \
         patch.object(settings, "portrait_llm_api_key", "test-key"), \
         patch("app.services.portrait_service.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_get_client.return_value = mock_client

        url = await generate_portrait("fail-id", "Nobody", "")

    assert url is None
    mock_client.post.assert_called_once()


@pytest.mark.anyio
async def test_generate_portrait_unconfigured_returns_none():
    """Without PORTRAIT_LLM_BASE_URL / PORTRAIT_LLM_API_KEY the service must
    degrade gracefully offline: no HTTP call, just None."""
    from app.config import settings

    with patch.object(settings, "portrait_llm_base_url", None), \
         patch.object(settings, "portrait_llm_api_key", None), \
         patch("app.services.portrait_service.get_client") as mock_get_client:
        url = await generate_portrait("no-conf-id", "Nobody", "")

    assert url is None
    mock_get_client.assert_not_called()
