"""Internal HTTP client that preserves the public Agent Player API contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from app.config import settings


class HostedAgentApiError(RuntimeError):
    def __init__(self, code: str, status_code: int, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


@dataclass
class _Session:
    token: SecretStr
    expires_at: datetime


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class HostedAgentApiClient:
    """Caches short-lived sessions in worker memory; play tokens never leave it."""

    def __init__(self) -> None:
        self._base = settings.hosted_agent_runner_internal_api_base.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(max(30.0, float(settings.user_llm_timeout) + 30.0)),
        )
        self._sessions: dict[str, _Session] = {}

    async def aclose(self) -> None:
        await self._client.aclose()
        for session in self._sessions.values():
            session.token = SecretStr("")
        self._sessions.clear()

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        token: SecretStr,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        operation_timeout: float = 35.0,
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(operation_timeout):
                response = await self._client.request(
                    method,
                    path,
                    headers={
                        "Authorization": f"Bearer {token.get_secret_value()}",
                        **(headers or {}),
                    },
                    json=json_body,
                    timeout=httpx.Timeout(max(1.0, operation_timeout - 1.0)),
                )
        except (asyncio.TimeoutError, httpx.HTTPError) as exc:
            raise HostedAgentApiError("agent_api_unavailable", 503, True) from exc
        if len(response.content) > 1024 * 1024:
            raise HostedAgentApiError("agent_api_response_too_large", 502)
        if response.status_code >= 400:
            code = f"agent_api_http_{response.status_code}"
            try:
                detail = response.json().get("detail")
                if isinstance(detail, dict) and isinstance(detail.get("code"), str):
                    code = detail["code"]
            except (ValueError, AttributeError):
                pass
            raise HostedAgentApiError(
                code,
                response.status_code,
                response.status_code >= 500 or response.status_code in {409, 429},
            )
        try:
            parsed = response.json()
        except ValueError as exc:
            raise HostedAgentApiError("agent_api_response_invalid", 502) from exc
        if not isinstance(parsed, dict):
            raise HostedAgentApiError("agent_api_response_invalid", 502)
        return parsed

    async def session(self, *, profile_id: str, play_token: SecretStr) -> SecretStr:
        cached = self._sessions.get(profile_id)
        if cached is not None and cached.expires_at > datetime.now(UTC) + timedelta(seconds=90):
            return cached.token
        parsed = await self._json_request(
            "POST",
            "/api/v1/agent-sessions",
            token=play_token,
            json_body={"client": {"name": "simverse-hosted-agent", "version": "1"}},
        )
        raw_token = parsed.get("session_token")
        raw_expiry = parsed.get("expires_at")
        if not isinstance(raw_token, str) or not raw_token:
            raise HostedAgentApiError("agent_session_invalid", 502)
        try:
            expiry = _aware(datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00")))
        except (TypeError, ValueError) as exc:
            raise HostedAgentApiError("agent_session_invalid", 502) from exc
        session = _Session(token=SecretStr(raw_token), expires_at=expiry)
        old = self._sessions.get(profile_id)
        if old is not None:
            old.token = SecretStr("")
        self._sessions[profile_id] = session
        return session.token

    async def observe(self, *, session_token: SecretStr) -> dict[str, Any]:
        return await self._json_request(
            "GET", "/api/v1/agent/observation", token=session_token
        )

    async def daily_reward(self, *, session_token: SecretStr) -> dict[str, Any]:
        return await self._json_request(
            "POST", "/api/v1/agent/daily-reward", token=session_token
        )

    def invalidate_session(self, profile_id: str) -> None:
        session = self._sessions.pop(profile_id, None)
        if session is not None:
            session.token = SecretStr("")

    async def action(
        self,
        *,
        session_token: SecretStr,
        action_id: str,
        observation_seq: int,
        action_type: str,
        params: dict[str, Any],
        fence_headers: dict[str, str],
    ) -> dict[str, Any]:
        return await self._json_request(
            "POST",
            "/api/v1/agent/actions",
            token=session_token,
            headers=fence_headers,
            json_body={
                "action_id": action_id,
                "observation_seq": observation_seq,
                "type": action_type,
                "params": params,
            },
        )

    async def npc_chat(
        self,
        *,
        session_token: SecretStr,
        turn_id: str,
        observation_seq: int,
        resident_slug: str,
        text: str,
        fence_headers: dict[str, str],
    ) -> dict[str, Any]:
        return await self._json_request(
            "POST",
            "/api/v1/agent/npc-chat-turns",
            token=session_token,
            headers=fence_headers,
            json_body={
                "turn_id": turn_id,
                "observation_seq": observation_seq,
                "resident_slug": resident_slug,
                "text": text,
            },
            operation_timeout=max(35.0, float(settings.user_llm_timeout) + 35.0),
        )
