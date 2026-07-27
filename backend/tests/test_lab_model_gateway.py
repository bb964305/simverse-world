import json
import time
from decimal import Decimal

import httpx
import jwt
import pytest

from app.lab.model_gateway.config import GatewayConfig
from app.lab.model_gateway.ledger import UsageLedger
from app.lab.model_gateway.service import create_app
from app.lab.model_policy import MODEL_GATEWAY_AUDIENCE, MODEL_GATEWAY_ISSUER


SECRET = "gateway-test-secret-that-is-at-least-32-bytes"


def _config() -> GatewayConfig:
    return GatewayConfig(
        bind_host="127.0.0.1", bind_port=8096,
        upstream_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        upstream_api_key="upstream-test-key", auth_secret=SECRET,
        ledger_path=":memory:", request_timeout_s=10,
        max_request_bytes=1024 * 1024, default_max_output_tokens=1024,
        cny_per_usd=Decimal("7"),
        flash_input_cny_per_million=Decimal("1"),
        flash_output_cny_per_million=Decimal("2"),
        pro_input_cny_per_million=Decimal("12"),
        pro_output_cny_per_million=Decimal("24"),
    )


def _token(*, tier="low", model="deepseek-v4-flash", run_id="run-1") -> str:
    now = int(time.time())
    resources = {"low": (2, 2048), "high": (4, 4096)}[tier]
    return jwt.encode({
        "iss": MODEL_GATEWAY_ISSUER, "aud": MODEL_GATEWAY_AUDIENCE,
        "sub": run_id, "jti": "jti-" + run_id,
        "tenant_id": "tenant", "task_id": "task", "run_id": run_id,
        "model_tier": tier, "model": model,
        "model_policy_version": "test-policy", "budget_usd_cents": 50,
        "resource_cpu_cores": resources[0], "resource_memory_mb": resources[1],
        "max_model_tokens": 10_000, "iat": now, "nbf": now, "exp": now + 600,
    }, SECRET, algorithm="HS256")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_custom_tool_round_trip_preserves_reasoning_and_usage():
    calls: list[dict] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            assert body["model"] == "deepseek-v4-flash"
            assert body["tool_choice"] == "auto"
            assert body["tools"][0]["function"]["name"] == "shell"
            return httpx.Response(200, json={
                "choices": [{"message": {
                    "role": "assistant", "content": None,
                    "reasoning_content": "Need inspect architecture.",
                    "tool_calls": [{"id": "call-1", "type": "function",
                        "function": {"name": "shell", "arguments": "{\"input\":\"uname -m\"}"}}],
                }}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                    "completion_tokens_details": {"reasoning_tokens": 10}},
            })
        assistant = next(m for m in body["messages"] if m["role"] == "assistant")
        tool = next(m for m in body["messages"] if m["role"] == "tool")
        assert assistant["reasoning_content"] == "Need inspect architecture."
        assert assistant["tool_calls"][0]["id"] == "call-1"
        assert tool == {"role": "tool", "tool_call_id": "call-1", "content": "aarch64"}
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "aarch64 OK"}}],
            "usage": {"prompt_tokens": 130, "completion_tokens": 12,
                "completion_tokens_details": {"reasoning_tokens": 4}},
        })

    config = _config()
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        first = await client.post("/v1/responses", headers=_headers(_token()), json={
            "model": "lab-auto", "stream": False,
            "input": "Report the architecture",
            "tools": [{"type": "custom", "name": "shell", "description": "run shell"}],
            "tool_choice": "required",
        })
        assert first.status_code == 200, first.text
        tool_call = first.json()["output"][0]
        assert tool_call["type"] == "custom_tool_call"
        assert tool_call["input"] == "uname -m"

        second = await client.post("/v1/responses", headers=_headers(_token()), json={
            "model": "lab-auto", "stream": False,
            "input": [tool_call, {
                "type": "custom_tool_call_output", "call_id": "call-1", "output": "aarch64",
            }],
            "tools": [{"type": "custom", "name": "shell", "description": "run shell"}],
        })
        assert second.status_code == 200, second.text
        assert second.json()["output"][0]["content"][0]["text"] == "aarch64 OK"

        usage = await client.get("/v1/lab/usage", headers=_headers(_token()))
        assert usage.json()["input_tokens"] == 230
        assert usage.json()["output_tokens"] == 32
        assert usage.json()["reasoning_tokens"] == 14
        assert usage.json()["request_count"] == 2
    await upstream_client.aclose()
    ledger.close()


@pytest.mark.anyio
async def test_namespace_tool_is_flattened_and_restored_across_turns():
    calls: list[dict] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            function = body["tools"][0]["function"]
            assert function["name"] == "collaboration__spawn_agent"
            assert function["parameters"]["required"] == ["task"]
            return httpx.Response(200, json={
                "choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "call-ns", "type": "function",
                        "function": {
                            "name": "collaboration__spawn_agent",
                            "arguments": "{\"task\":\"inspect\"}",
                        },
                    }],
                }}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
        assistant = next(m for m in body["messages"] if m["role"] == "assistant")
        assert assistant["tool_calls"][0]["function"] == {
            "name": "collaboration__spawn_agent",
            "arguments": "{\"task\":\"inspect\"}",
        }
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
        })

    config = _config()
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    token = _token(run_id="namespace-run")
    namespace_tool = {
        "type": "namespace", "name": "collaboration",
        "description": "Manage helper agents.",
        "tools": [{
            "type": "function", "name": "spawn_agent",
            "description": "Start an agent.",
            "parameters": {
                "type": "object", "properties": {"task": {"type": "string"}},
                "required": ["task"], "additionalProperties": False,
            },
        }],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        first = await client.post("/v1/responses", headers=_headers(token), json={
            "model": "lab-auto", "input": "delegate",
            "tools": [namespace_tool, {
                "type": "web_search", "external_web_access": False,
            }],
        })
        assert first.status_code == 200, first.text
        tool_call = first.json()["output"][0]
        assert tool_call["name"] == "spawn_agent"
        assert tool_call["namespace"] == "collaboration"

        second = await client.post("/v1/responses", headers=_headers(token), json={
            "model": "lab-auto",
            "input": [tool_call, {
                "type": "function_call_output", "call_id": "call-ns", "output": "ok",
            }],
            "tools": [namespace_tool],
        })
        assert second.status_code == 200, second.text
        assert second.json()["output"][0]["content"][0]["text"] == "done"
    await upstream_client.aclose()
    ledger.close()


@pytest.mark.anyio
async def test_high_tier_routes_pro_and_streams_responses_events():
    def upstream(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "deepseek-v4-pro"
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        })

    config = _config()
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    token = _token(tier="high", model="deepseek-v4-pro", run_id="pro-run")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.post("/v1/responses", headers=_headers(token), json={
            "model": "lab-auto", "stream": True, "input": "finish"
        })
        assert response.status_code == 200
        events = [line for line in response.text.splitlines() if line.startswith("event: ")]
        assert "event: response.created" in events
        assert "event: response.output_text.delta" in events
        assert "event: response.completed" in events
    await upstream_client.aclose()
    ledger.close()


@pytest.mark.anyio
async def test_client_cannot_override_model():
    config = _config()
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None))
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.post("/v1/responses", headers=_headers(_token()), json={
            "model": "deepseek-v4-pro", "input": "override"
        })
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "model_override_denied"
    await upstream_client.aclose()
    ledger.close()
