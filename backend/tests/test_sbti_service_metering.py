from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.sbti_service import (
    DIMENSION_CODES,
    SBTI_LAYER_MAX_CHARS,
    SBTI_NAME_MAX_CHARS,
    compute_sbti,
)


def _response(text: str):
    response = MagicMock()
    block = MagicMock()
    block.text = text
    response.content = [block]
    response.usage = None
    return response


@pytest.mark.anyio
async def test_compute_sbti_truncates_input_and_attributes_usage_to_user():
    dimensions = "{" + ",".join(f'"{code}":"M"' for code in DIMENSION_CODES) + "}"
    response = _response(dimensions)
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    usage = AsyncMock()

    with patch("app.services.sbti_service.get_client", return_value=client), \
         patch("app.services.sbti_service.record_usage", usage):
        result = await compute_sbti(
            "N" * (SBTI_NAME_MAX_CHARS + 20),
            "A" * (SBTI_LAYER_MAX_CHARS + 20),
            "P" * (SBTI_LAYER_MAX_CHARS + 20),
            "S" * (SBTI_LAYER_MAX_CHARS + 20),
            user_id="user-123",
            conversation_id="forge-123",
        )

    assert result is not None
    prompt = client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "N" * SBTI_NAME_MAX_CHARS in prompt
    assert "N" * (SBTI_NAME_MAX_CHARS + 1) not in prompt
    assert "A" * SBTI_LAYER_MAX_CHARS in prompt
    assert "A" * (SBTI_LAYER_MAX_CHARS + 1) not in prompt
    assert "P" * (SBTI_LAYER_MAX_CHARS + 1) not in prompt
    assert "S" * (SBTI_LAYER_MAX_CHARS + 1) not in prompt

    usage.assert_awaited_once()
    kwargs = usage.await_args.kwargs
    assert kwargs["response"] is response
    assert kwargs["user_id"] == "user-123"
    assert kwargs["conversation_id"] == "forge-123"
    assert kwargs["parse_ok"] is True
    assert kwargs["est_input_tokens"] > 0


@pytest.mark.anyio
async def test_compute_sbti_meters_failed_llm_attempt_with_estimate():
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("upstream failed"))
    usage = AsyncMock()

    with patch("app.services.sbti_service.get_client", return_value=client), \
         patch("app.services.sbti_service.record_usage", usage):
        result = await compute_sbti(
            "Failure",
            "A" * 60,
            "",
            "",
            user_id="user-456",
        )

    assert result is None
    usage.assert_awaited_once()
    kwargs = usage.await_args.kwargs
    assert kwargs["response"] is None
    assert kwargs["user_id"] == "user-456"
    assert kwargs["parse_ok"] is False
    assert kwargs["est_input_tokens"] > 0
    assert kwargs["est_output_tokens"] == 0


@pytest.mark.anyio
async def test_compute_sbti_does_not_meter_when_short_input_skips_llm():
    usage = AsyncMock()

    with patch("app.services.sbti_service.get_client") as get_client, \
         patch("app.services.sbti_service.record_usage", usage):
        result = await compute_sbti("Short", "tiny", "", "", user_id="user-789")

    assert result is None
    get_client.assert_not_called()
    usage.assert_not_awaited()
