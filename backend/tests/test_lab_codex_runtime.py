import asyncio
from pathlib import Path

import httpx
import pytest

from app.lab.codex_runtime.config import CodexRuntimeConfig
from app.lab.codex_runtime.service import RuntimeResourcePool, _codex_config, create_app


API_KEY = "codex-runtime-test-key-that-is-at-least-32-bytes"


@pytest.mark.anyio
async def test_codex_runtime_exposes_lab_adapter_protocol(tmp_path, monkeypatch):
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'uname -m'}}))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'uname -m','aggregated_output':'aarch64\\n','exit_code':0}}))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ARM task complete'}}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    config = CodexRuntimeConfig(
        bind_host="127.0.0.1", bind_port=8097, api_key=API_KEY,
        codex_binary=str(executable), workspace_root=str(tmp_path / "runs"),
        max_active_runs=1, run_timeout_s=10, max_step_text_chars=1000,
    )

    async def fake_usage(_session):
        return {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                "cost_usd_cents": 1}

    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_usage", fake_usage)
    app = create_app(config)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        created = await client.post("/runs", headers=headers, json={
            "run_id": "run", "tenant_id": "tenant", "scopes": ["code"],
            "budget_usd": 0.25, "egress_allowlist": [], "model_tier": "low",
            "model_name": "deepseek-v4-flash", "model_policy_version": "test",
            "resource_cpu_cores": 2, "resource_memory_mb": 2048,
            "model_gateway_base_url": "http://gateway/v1",
            "model_gateway_token": "run-token",
        })
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        submitted = await client.post(
            f"/runs/{session_id}/goal", headers=headers,
            json={"brief": "check architecture", "scopes": ["code"]},
        )
        assert submitted.status_code == 200
        result = None
        for _ in range(20):
            result = await client.get(
                f"/runs/{session_id}/steps", headers=headers, params={"after": 0}
            )
            if result.json()["done"]:
                break
            await asyncio.sleep(0.01)
        assert result is not None and result.json()["done"] is True
        steps = result.json()["steps"]
        assert [step["phase"] for step in steps] == [
            "tool_call", "observation", "message", "message"
        ]
        assert steps[-1]["model_tokens"] == 15
        assert steps[-1]["cost_usd_cents"] == 1

        artifact = await client.get(f"/runs/{session_id}/artifacts", headers=headers)
        assert artifact.json()["artifacts"][0]["text_md"] == "ARM task complete"
        assert artifact.json()["artifacts"][0]["meta"]["resource_cpu_cores"] == 2
        stopped = await client.post(f"/runs/{session_id}/stop", headers=headers)
        assert stopped.status_code == 200
        assert not (Path(config.workspace_root) / session_id).exists()


def test_codex_shell_environment_excludes_gateway_token():
    rendered = _codex_config("http://gateway:8096/v1")
    assert 'wire_api = "responses"' in rendered
    assert 'inherit = "none"' in rendered
    assert 'exclude = ["LAB_RUN_TOKEN", "*KEY*", "*SECRET*", "*TOKEN*"]' in rendered


@pytest.mark.anyio
async def test_codex_runtime_rejects_non_code_scope(tmp_path):
    config = CodexRuntimeConfig(
        bind_host="127.0.0.1", bind_port=8097, api_key=API_KEY,
        codex_binary="/bin/false", workspace_root=str(tmp_path / "runs"),
        max_active_runs=1, run_timeout_s=10, max_step_text_chars=1000,
    )
    app = create_app(config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post("/runs", headers={"Authorization": f"Bearer {API_KEY}"}, json={
            "run_id": "run", "tenant_id": "tenant", "scopes": ["browse"],
            "budget_usd": 0.25, "egress_allowlist": [], "model_tier": "low",
            "model_name": "deepseek-v4-flash", "model_policy_version": "test",
            "resource_cpu_cores": 2, "resource_memory_mb": 2048,
            "model_gateway_base_url": "http://gateway/v1",
            "model_gateway_token": "run-token",
        })
        assert response.status_code == 403


@pytest.mark.anyio
async def test_runtime_rejects_resource_tier_escalation(tmp_path):
    config = CodexRuntimeConfig(
        bind_host="127.0.0.1", bind_port=8097, api_key=API_KEY,
        codex_binary="/bin/false", workspace_root=str(tmp_path / "runs"),
        max_active_runs=2, run_timeout_s=10, max_step_text_chars=1000,
    )
    app = create_app(config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post(
            "/runs", headers={"Authorization": f"Bearer {API_KEY}"}, json={
                "run_id": "run", "tenant_id": "tenant", "scopes": ["code"],
                "budget_usd": 0.25, "egress_allowlist": [], "model_tier": "low",
                "model_name": "deepseek-v4-flash", "model_policy_version": "test",
                "resource_cpu_cores": 4, "resource_memory_mb": 4096,
                "model_gateway_base_url": "http://gateway/v1",
                "model_gateway_token": "run-token",
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "resource tier mismatch"


@pytest.mark.anyio
async def test_resource_pool_allows_two_low_runs_or_one_high_run():
    pool = RuntimeResourcePool(max_runs=2, cpu_cores=4, memory_mb=8192)
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_low = asyncio.Event()
    high_entered = asyncio.Event()

    async def low(marker: asyncio.Event):
        async with pool.allocation(cpu_cores=2, memory_mb=2048):
            marker.set()
            await release_low.wait()

    async def high():
        async with pool.allocation(cpu_cores=4, memory_mb=4096):
            high_entered.set()

    low_tasks = [asyncio.create_task(low(first_entered)), asyncio.create_task(low(second_entered))]
    await asyncio.gather(first_entered.wait(), second_entered.wait())
    assert (pool.active_runs, pool.used_cpu_cores) == (2, 4)
    high_task = asyncio.create_task(high())
    await asyncio.sleep(0)
    assert not high_entered.is_set()
    release_low.set()
    await asyncio.gather(*low_tasks, high_task)
    assert high_entered.is_set()
    assert (pool.active_runs, pool.used_cpu_cores, pool.used_memory_mb) == (0, 0, 0)
