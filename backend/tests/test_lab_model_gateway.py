import asyncio
import json
import time
from decimal import Decimal

import httpx
import jwt
import pytest

from app.lab.model_gateway.config import GatewayConfig
from app.lab.model_gateway.ledger import RunRevokedError, UsageLedger
from app.lab.model_gateway.service import ReasoningStore, create_app
from app.lab.model_gateway.translation import responses_to_chat
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


def _token(
    *, tier="low", model="deepseek-v4-flash", run_id="run-1",
    max_model_tokens=10_000, budget_usd_cents=50,
) -> str:
    now = int(time.time())
    resources = {"low": (2, 2048), "high": (4, 4096)}[tier]
    return jwt.encode({
        "iss": MODEL_GATEWAY_ISSUER, "aud": MODEL_GATEWAY_AUDIENCE,
        "sub": run_id, "jti": "jti-" + run_id,
        "tenant_id": "tenant", "task_id": "task", "run_id": run_id,
        "model_tier": tier, "model": model,
        "model_policy_version": "test-policy", "budget_usd_cents": budget_usd_cents,
        "resource_cpu_cores": resources[0], "resource_memory_mb": resources[1],
        "max_model_tokens": max_model_tokens, "iat": now, "nbf": now, "exp": now + 600,
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


@pytest.mark.anyio
async def test_concurrent_requests_cannot_spend_the_same_token_budget():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def upstream(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},
        })

    config = _config()
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    token = _token(run_id="atomic-budget", max_model_tokens=1800)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        first_task = asyncio.create_task(client.post(
            "/v1/responses", headers=_headers(token),
            json={"model": "lab-auto", "input": "first", "max_output_tokens": 1000},
        ))
        await entered.wait()
        second = await client.post(
            "/v1/responses", headers=_headers(token),
            json={"model": "lab-auto", "input": "second", "max_output_tokens": 1000},
        )
        assert second.status_code == 402
        assert second.json()["error"]["code"] == "budget_exhausted"
        release.set()
        assert (await first_task).status_code == 200
        usage = (await client.get("/v1/lab/usage", headers=_headers(token))).json()
        assert usage["request_count"] == 1
        assert usage["inflight_requests"] == 0
        assert usage["reserved_tokens"] == 0
    await upstream_client.aclose()
    ledger.close()


@pytest.mark.anyio
async def test_per_run_inflight_limit_is_enforced():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def upstream(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    config = _config()
    object.__setattr__(config, "max_inflight_per_run", 1)
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    token = _token(run_id="inflight-limit")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        first_task = asyncio.create_task(client.post(
            "/v1/responses", headers=_headers(token),
            json={"model": "lab-auto", "input": "first", "max_output_tokens": 10},
        ))
        await entered.wait()
        second = await client.post(
            "/v1/responses", headers=_headers(token),
            json={"model": "lab-auto", "input": "second", "max_output_tokens": 10},
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "inflight_exhausted"
        release.set()
        assert (await first_task).status_code == 200
    await upstream_client.aclose()
    ledger.close()


@pytest.mark.anyio
async def test_upstream_usage_is_recorded_before_response_translation():
    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [],
            "usage": {"prompt_tokens": 17, "completion_tokens": 3},
        })

    config = _config()
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    token = _token(run_id="translation-failed")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/v1/responses", headers=_headers(token),
            json={"model": "lab-auto", "input": "invalid upstream"},
        )
        assert response.status_code == 502
        usage = (await client.get("/v1/lab/usage", headers=_headers(token))).json()
        assert usage["input_tokens"] == 17
        assert usage["output_tokens"] == 3
        assert usage["request_count"] == 1
    await upstream_client.aclose()
    ledger.close()


@pytest.mark.anyio
async def test_ambiguous_upstream_transport_failure_revokes_unknown_cost_run():
    def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider outcome is unknown", request=request)

    config = _config()
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    token = _token(run_id="ambiguous-transport")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/v1/responses", headers=_headers(token),
            json={"model": "lab-auto", "input": "ambiguous provider request"},
        )
        assert response.status_code == 504
        usage = (await client.get("/v1/lab/usage", headers=_headers(token))).json()
        assert usage["cost_unknown"] is True
        assert usage["revoked"] is True
        denied = await client.post(
            "/v1/responses", headers=_headers(token),
            json={"model": "lab-auto", "input": "must not retry"},
        )
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "run_revoked"
    await upstream_client.aclose()
    ledger.close()


@pytest.mark.anyio
async def test_revoked_run_cannot_make_another_request():
    config = _config()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("upstream must not be called"))
    )
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    token = _token(run_id="revoked-run")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        revoked = await client.post("/v1/lab/revoke", headers=_headers(token))
        assert revoked.status_code == 200
        assert revoked.json()["revoked"] is True
        response = await client.post(
            "/v1/responses", headers=_headers(token),
            json={"model": "lab-auto", "input": "spend after terminal"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "run_revoked"
    await upstream_client.aclose()
    ledger.close()


@pytest.mark.anyio
async def test_active_run_token_can_renew_but_revoked_run_cannot():
    config = _config()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("upstream must not be called"))
    )
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    token = _token(run_id="renewable-run")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        renewed = await client.post("/v1/lab/renew", headers=_headers(token))
        assert renewed.status_code == 200
        renewed_token = renewed.json()["token"]
        assert renewed_token != token
        assert renewed.json()["expires_in_s"] == 300
        await client.post("/v1/lab/revoke", headers=_headers(renewed_token))
        denied = await client.post("/v1/lab/renew", headers=_headers(renewed_token))
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "run_revoked"
    await upstream_client.aclose()
    ledger.close()


def test_reasoning_store_is_run_scoped_bounded_and_expires(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("app.lab.model_gateway.service.time.monotonic", lambda: now[0])
    store = ReasoningStore(ttl_s=5, max_entries=2)
    store.put_many("run-a", {"same-call": "private-a"})
    assert store.get("run-a", "same-call") == "private-a"
    assert store.get("run-b", "same-call") is None
    store.put_many("run-a", {"second": "two", "third": "three"})
    assert store.get("run-a", "same-call") is None
    now[0] = 16.0
    assert store.get("run-a", "third") is None


def test_restart_marks_stranded_reservation_unknown_and_revoked(tmp_path):
    ledger_path = str(tmp_path / "usage.db")
    config = _config()
    first = UsageLedger(ledger_path, config)
    first.reserve(
        run_id="interrupted-run",
        model="deepseek-v4-flash",
        estimated_input_tokens=100,
        max_output_tokens=200,
        max_model_tokens=1_000,
        budget_usd_cents=50,
        max_inflight_requests=1,
    )
    first.close()

    recovered = UsageLedger(ledger_path, config)
    totals = recovered.get("interrupted-run", "deepseek-v4-flash")
    assert totals.cost_unknown is True
    assert totals.revoked is True
    assert totals.inflight_requests == 0
    assert totals.reserved_tokens == 0
    with pytest.raises(RunRevokedError, match="revoked"):
        recovered.reserve(
            run_id="interrupted-run",
            model="deepseek-v4-flash",
            estimated_input_tokens=1,
            max_output_tokens=1,
            max_model_tokens=1_000,
            budget_usd_cents=50,
            max_inflight_requests=1,
        )
    recovered.close()


def test_live_peer_does_not_recover_another_instance_reservation(tmp_path):
    ledger_path = str(tmp_path / "usage.db")
    config = _config()
    first = UsageLedger(ledger_path, config)
    reservation_id = first.reserve(
        run_id="live-peer-run",
        model="deepseek-v4-flash",
        estimated_input_tokens=100,
        max_output_tokens=200,
        max_model_tokens=1_000,
        budget_usd_cents=50,
        max_inflight_requests=1,
    )

    peer = UsageLedger(ledger_path, config)
    totals = peer.get("live-peer-run", "deepseek-v4-flash")
    assert totals.cost_unknown is False
    assert totals.revoked is False
    assert totals.inflight_requests == 1
    assert totals.reserved_tokens == 300

    first.release(reservation_id)
    peer.close()
    first.close()


def test_expired_peer_reservation_recovery_commits_before_revocation(
    tmp_path, monkeypatch
):
    now = [100]
    monkeypatch.setattr("app.lab.model_gateway.ledger.time.time", lambda: now[0])
    ledger_path = str(tmp_path / "usage.db")
    config = _config()
    first = UsageLedger(ledger_path, config)
    first.reserve(
        run_id="expired-peer-run",
        model="deepseek-v4-flash",
        estimated_input_tokens=100,
        max_output_tokens=200,
        max_model_tokens=1_000,
        budget_usd_cents=50,
        max_inflight_requests=1,
    )
    now[0] = 101
    peer = UsageLedger(ledger_path, config)
    now[0] = 200

    with pytest.raises(RunRevokedError, match="revoked"):
        peer.reserve(
            run_id="expired-peer-run",
            model="deepseek-v4-flash",
            estimated_input_tokens=1,
            max_output_tokens=1,
            max_model_tokens=1_000,
            budget_usd_cents=50,
            max_inflight_requests=1,
        )

    totals = peer.get("expired-peer-run", "deepseek-v4-flash")
    assert totals.cost_unknown is True
    assert totals.revoked is True
    assert totals.inflight_requests == 0
    assert totals.reserved_tokens == 0
    peer.close()
    first.close()


@pytest.mark.anyio
async def test_unclassified_upstream_exception_marks_reservation_unknown():
    def upstream(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected client or serialization failure")

    config = _config()
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    ledger = UsageLedger(":memory:", config)
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    token = _token(run_id="unexpected-upstream-error")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        with pytest.raises(RuntimeError, match="unexpected client"):
            await client.post(
                "/v1/responses",
                headers=_headers(token),
                json={"model": "lab-auto", "input": "trigger unexpected failure"},
            )

    totals = ledger.get("unexpected-upstream-error", "deepseek-v4-flash")
    assert totals.cost_unknown is True
    assert totals.revoked is True
    assert totals.inflight_requests == 0
    assert totals.reserved_tokens == 0
    await upstream_client.aclose()
    ledger.close()


def test_reasoning_injection_is_deduplicated_and_byte_bounded():
    body = {
        "model": "lab-auto",
        "input": [
            {
                "type": "function_call",
                "call_id": "repeated-call",
                "name": "shell",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "call_id": "repeated-call",
                "name": "shell",
                "arguments": "{}",
            },
        ],
        "tools": [{"type": "function", "name": "shell"}],
    }

    chat, _registry = responses_to_chat(
        body,
        model="deepseek-v4-pro",
        max_output_tokens=100,
        reasoning_for_call=lambda _call_id: "abcdefghijklmnop",
        max_reasoning_bytes=8,
    )

    injected = [
        message["reasoning_content"]
        for message in chat["messages"]
        if "reasoning_content" in message
    ]
    assert injected == ["abcdefgh"]
    assert sum(len(value.encode("utf-8")) for value in injected) <= 8


@pytest.mark.anyio
async def test_budget_reservation_estimates_translated_chat_bytes(monkeypatch):
    config = _config()
    object.__setattr__(config, "reasoning_max_injected_bytes", 4096)
    observed: list[int] = []
    ledger = UsageLedger(":memory:", config)
    original_reserve = ledger.reserve

    def capture_reserve(**kwargs):
        observed.append(kwargs["estimated_input_tokens"])
        return original_reserve(**kwargs)

    monkeypatch.setattr(ledger, "reserve", capture_reserve)
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        }))
    )
    app = create_app(config, http_client=upstream_client, ledger=ledger)
    app.state.reasoning.put_many("translated-estimate", {"call-1": "x" * 4000})
    token = _token(run_id="translated-estimate", max_model_tokens=20_000)
    body = {
        "model": "lab-auto",
        "input": [{
            "type": "function_call",
            "call_id": "call-1",
            "name": "shell",
            "arguments": "{}",
        }],
        "tools": [{"type": "function", "name": "shell"}],
        "max_output_tokens": 100,
    }
    raw_size = len(json.dumps(body, separators=(",", ":")).encode("utf-8"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/v1/responses", headers=_headers(token), content=json.dumps(body)
        )
        assert response.status_code == 200, response.text

    assert observed and observed[0] > raw_size + 3500
    await upstream_client.aclose()
    ledger.close()
