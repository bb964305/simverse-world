"""Authenticated Runner client for durable egress actions."""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from app.http import get_client

from .models import EgressActionCommand, EgressActionStatus


class RemoteEgressError(RuntimeError):
    pass


class RemoteEgressTransportError(RemoteEgressError):
    pass


class RemoteEgressProtocolError(RemoteEgressError):
    pass


class RemoteEgressClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        request_timeout_s: float = 30.0,
        action_timeout_s: float = 90.0,
        poll_interval_s: float = 0.25,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        try:
            parsed = httpx.URL(base_url)
        except Exception as exc:
            raise ValueError("invalid egress service base URL") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("egress service base URL must be HTTP(S)")
        # ``httpx.URL`` exposes absent credentials as empty strings, unlike
        # ``urllib.parse.SplitResult`` which uses ``None``.  Checking identity
        # here therefore rejected every normal credential-free service URL.
        if parsed.username or parsed.password:
            raise ValueError("egress service base URL cannot contain credentials")
        if len(api_key) < 32:
            raise ValueError("egress service API key must contain at least 32 characters")
        if not 0.1 <= request_timeout_s <= 120:
            raise ValueError("egress request timeout is out of range")
        if not request_timeout_s <= action_timeout_s <= 600:
            raise ValueError("egress action timeout is out of range")
        if not 0.01 <= poll_interval_s <= 10:
            raise ValueError("egress poll interval is out of range")
        self.base_url = str(parsed).rstrip("/")
        self.api_key = api_key
        self.request_timeout_s = request_timeout_s
        self.action_timeout_s = action_timeout_s
        self.poll_interval_s = poll_interval_s
        self._http_client = http_client

    @classmethod
    def configured(cls) -> "RemoteEgressClient":
        base_url = os.getenv("LAB_EGRESS_BASE_URL", "").strip()
        api_key = os.getenv("LAB_EGRESS_API_KEY", "")
        if not base_url:
            raise ValueError("LAB_EGRESS_BASE_URL is not configured")
        return cls(
            base_url=base_url,
            api_key=api_key,
            request_timeout_s=float(os.getenv("LAB_EGRESS_REQUEST_TIMEOUT_S", "30")),
            action_timeout_s=float(os.getenv("LAB_EGRESS_ACTION_TIMEOUT_S", "90")),
            poll_interval_s=float(os.getenv("LAB_EGRESS_POLL_INTERVAL_S", "0.25")),
        )

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http_client or get_client()

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def ready(self, *, require_search: bool = False) -> bool:
        try:
            response = await self.http.get(
                f"{self.base_url}/readyz", timeout=self.request_timeout_s
            )
            if response.status_code != 200:
                return False
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        return bool(
            isinstance(payload, dict)
            and payload.get("ready") is True
            and payload.get("service") == "lab-egress"
            and payload.get("fetch_available") is True
            and (not require_search or payload.get("search_available") is True)
        )

    async def get(self, action_id: str) -> EgressActionStatus | None:
        try:
            response = await self.http.get(
                f"{self.base_url}/v1/actions/{action_id}",
                headers=self.headers,
                timeout=self.request_timeout_s,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RemoteEgressTransportError("egress status request failed") from exc
        if response.status_code == 404:
            return None
        return self._decode(response)

    async def submit(self, command: EgressActionCommand) -> EgressActionStatus:
        try:
            response = await self.http.post(
                f"{self.base_url}/v1/actions",
                headers=self.headers,
                json=command.model_dump(mode="json"),
                timeout=self.request_timeout_s,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RemoteEgressTransportError("egress submit request failed") from exc
        return self._decode(response, expected_digest=command.request_digest)

    async def execute(self, command: EgressActionCommand) -> EgressActionStatus:
        deadline = time.monotonic() + self.action_timeout_s
        while True:
            status = await self.submit(command)
            if status.state in {"succeeded", "failed"}:
                return status
            if time.monotonic() >= deadline:
                raise RemoteEgressTransportError("egress action timed out")
            await asyncio.sleep(self.poll_interval_s)

    @staticmethod
    def _decode(
        response: httpx.Response, *, expected_digest: str | None = None
    ) -> EgressActionStatus:
        if response.status_code not in {200, 202}:
            if response.status_code == 409:
                raise RemoteEgressProtocolError("egress action binding conflict")
            raise RemoteEgressProtocolError(
                f"egress service returned HTTP {response.status_code}"
            )
        try:
            status = EgressActionStatus.model_validate(response.json())
        except Exception as exc:
            raise RemoteEgressProtocolError("invalid egress service response") from exc
        if expected_digest is not None and status.request_digest != expected_digest:
            raise RemoteEgressProtocolError("egress request digest changed")
        return status
