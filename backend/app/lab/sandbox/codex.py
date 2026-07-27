"""HTTP adapter for the isolated Codex runtime deployed on the ARM worker."""
from __future__ import annotations

from typing import AsyncIterator

from app.config import settings
from app.lab.sandbox.base import HttpAgentAdapter, HttpHandle, RunSpec, StepEvent


class CodexAdapter(HttpAgentAdapter):
    name = "codex"

    def __init__(self) -> None:
        super().__init__(
            base_url=settings.lab_codex_base_url,
            api_key=settings.lab_codex_api_key,
            timeout=max(60.0, float(settings.lab_budget_wall_clock_ms) / 1000.0),
        )

    async def start(self, spec: RunSpec) -> HttpHandle:
        self._require_configured()
        from app.http import get_client

        response = await get_client().post(
            f"{self.base_url}/runs",
            headers=self._headers(),
            timeout=self.timeout,
            json={
                "run_id": spec.run_id,
                "tenant_id": spec.tenant_id,
                "scopes": spec.scopes,
                "budget_usd": spec.budget_usd,
                "egress_allowlist": spec.egress_allowlist,
                "model_tier": spec.model_tier,
                "model_name": spec.model_name,
                "model_policy_version": spec.model_policy_version,
                "resource_cpu_cores": spec.resource_cpu_cores,
                "resource_memory_mb": spec.resource_memory_mb,
                "model_gateway_base_url": spec.model_gateway_base_url,
                "model_gateway_token": spec.model_gateway_token,
            },
        )
        response.raise_for_status()
        session_id = (response.json() or {}).get("session_id", spec.run_id)
        return HttpHandle(self, session_id, spec)

    def step_stream(self, handle: HttpHandle) -> AsyncIterator[StepEvent]:
        return self._step_stream(handle)

    async def _step_stream(self, handle: HttpHandle) -> AsyncIterator[StepEvent]:
        from app.http import get_client

        after = 0
        while True:
            response = await get_client().get(
                f"{self.base_url}/runs/{handle.session_id}/steps",
                headers=self._headers(),
                timeout=self.timeout,
                params={"after": after},
            )
            response.raise_for_status()
            data = response.json() or {}
            for raw in data.get("steps", []):
                after = max(after, int(raw.get("seq", after)))
                yield StepEvent(
                    phase=raw.get("phase", "message"),
                    summary=raw.get("summary", ""),
                    tool=raw.get("tool"),
                    payload=raw.get("payload") or {},
                    cost_usd_cents=int(raw.get("cost_usd_cents", 0)),
                    model_tokens=int(raw.get("model_tokens", 0)),
                )
            if data.get("done"):
                if data.get("failed"):
                    raise RuntimeError(data.get("error") or "Codex runtime failed")
                break
