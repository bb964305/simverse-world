"""Tests for the WS auth handshake (P0-4c: token in first message, not URL)."""
import json

import pytest

from app.services.auth_service import create_token
from app.ws.handlers.connection import _authenticate


class FakeWS:
    """Minimal stand-in for an accepted Starlette WebSocket."""

    def __init__(self, query_token: str | None = None, messages: list[str] | None = None):
        self.query_params = {"token": query_token} if query_token is not None else {}
        self._messages = list(messages or [])

    async def receive_text(self) -> str:
        assert self._messages, "test sent no messages but handler asked for one"
        return self._messages.pop(0)


@pytest.mark.anyio
async def test_auth_message_with_valid_token():
    token = create_token("user-42")
    ws = FakeWS(messages=[json.dumps({"type": "auth", "token": token})])
    assert await _authenticate(ws) == "user-42"


@pytest.mark.anyio
async def test_auth_message_with_invalid_token_rejected():
    ws = FakeWS(messages=[json.dumps({"type": "auth", "token": "not-a-jwt"})])
    assert await _authenticate(ws) is None


@pytest.mark.anyio
async def test_first_message_not_auth_rejected():
    ws = FakeWS(messages=[json.dumps({"type": "move", "x": 1, "y": 2})])
    assert await _authenticate(ws) is None


@pytest.mark.anyio
async def test_malformed_first_message_rejected():
    ws = FakeWS(messages=["not json {"])
    assert await _authenticate(ws) is None


@pytest.mark.anyio
async def test_query_param_fallback_still_works():
    token = create_token("user-legacy")
    ws = FakeWS(query_token=token)
    assert await _authenticate(ws) == "user-legacy"


@pytest.mark.anyio
async def test_query_param_with_invalid_token_rejected():
    ws = FakeWS(query_token="garbage")
    assert await _authenticate(ws) is None
