import asyncio
import errno
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.lab.codex_runtime.config import CodexRuntimeConfig
from app.lab.codex_runtime.credential_proxy import RunCredentialProxy
from app.lab.codex_runtime.launcher import main as launcher_main
from app.lab.codex_runtime.service import (
    CreateRun,
    Goal,
    RuntimeResourcePool,
    RuntimeSession,
    _consume_codex,
    _codex_config,
    _create_run_cgroup,
    _has_hidepid_2,
    _remove_workspace,
    create_app,
)


API_KEY = "codex-runtime-test-key-that-is-at-least-32-bytes"


def test_remove_workspace_does_not_follow_external_directory_symlink(
    tmp_path, monkeypatch
):
    root = tmp_path / "runs"
    workspace = root / "run"
    outside = tmp_path / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    def simulated_chown(path, _uid, _gid, *, follow_symlinks=True):
        if Path(path).is_symlink() and follow_symlinks:
            raise FileNotFoundError(errno.ENOENT, "followed dangling link", str(path))

    monkeypatch.setattr(
        "app.lab.codex_runtime.service.os.chown", simulated_chown
    )

    _remove_workspace(workspace, str(root), restore_controller_owner=True)

    assert not workspace.exists()


def test_remove_workspace_continues_after_unrecoverable_chown(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    workspace = root / "run"
    blocked = workspace / "blocked"
    blocked.mkdir(parents=True)

    def selective_chown(path, uid, gid, **kwargs):
        if Path(path) == blocked:
            raise OSError(errno.EROFS, "read-only target", str(path))

    monkeypatch.setattr("app.lab.codex_runtime.service.os.chown", selective_chown)

    _remove_workspace(workspace, str(root), restore_controller_owner=True)

    assert not workspace.exists()


def test_remove_workspace_recovers_mode_zero_directory(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    workspace = root / "run"
    blocked = workspace / "blocked"
    blocked.mkdir(parents=True)
    (blocked / "residue").write_text("owned by run", encoding="utf-8")
    blocked.chmod(0)
    monkeypatch.setattr(
        "app.lab.codex_runtime.service.os.chown",
        lambda _path, _uid, _gid, **_kwargs: None,
    )
    try:
        _remove_workspace(workspace, str(root), restore_controller_owner=True)
        assert not workspace.exists()
    finally:
        if blocked.exists():
            blocked.chmod(0o700)


def test_run_cgroup_name_is_random_and_not_the_workspace_identifier(tmp_path):
    cgroup_root = tmp_path / "cgroups"
    workspace = tmp_path / "workspace-visible-id"
    cgroup_root.mkdir()
    workspace.mkdir()
    request = CreateRun(
        run_id="cgroup-name", tenant_id="tenant", scopes=["code"],
        budget_usd=1.0, egress_allowlist=[], model_tier="low",
        model_name="deepseek-v4-flash", model_policy_version="test",
        resource_cpu_cores=2, resource_memory_mb=2048,
        model_gateway_base_url="http://ignored/v1",
        model_gateway_token="run-token",
    )
    session = RuntimeSession("cgroup-name", request, workspace)
    config = CodexRuntimeConfig(
        bind_host="127.0.0.1", bind_port=8097, api_key=API_KEY,
        codex_binary="/bin/false", workspace_root=str(tmp_path),
        max_active_runs=1, run_timeout_s=10, max_step_text_chars=1000,
        model_gateway_base_url="http://trusted-gateway:8096/v1",
        enforce_process_isolation=True, cgroup_root=str(cgroup_root),
    )

    path = _create_run_cgroup(session, config)

    assert path is not None
    assert path.name.startswith("run-")
    assert path.name != workspace.name
    assert path.stat().st_mode & 0o777 == 0o700
    assert (path / "cpu.max").read_text(encoding="ascii") == "200000 100000"
    assert (path / "memory.max").read_text(encoding="ascii") == str(2048 * 1024 * 1024)


def test_launcher_rejects_system_uid_before_entering_isolation(monkeypatch):
    entered = []
    monkeypatch.setattr(
        "sys.argv",
        [
            "launcher", "--uid", "9999", "--gid", "10000",
            "--cgroup-path", "/cgroup", "--cwd", "/workspace",
            "--", "/bin/true",
        ],
    )
    monkeypatch.setattr(
        "app.lab.codex_runtime.launcher._enter_isolation",
        lambda **kwargs: entered.append(kwargs),
    )

    with pytest.raises(SystemExit, match="invalid Codex launcher isolation profile"):
        launcher_main()
    assert entered == []


def test_launcher_enters_isolation_then_executes_exact_command(monkeypatch):
    entered = []
    executed = []
    monkeypatch.setattr(
        "sys.argv",
        [
            "launcher", "--uid", "20001", "--gid", "20001",
            "--cgroup-path", "/cgroup/run-random", "--cwd", "/workspace",
            "--", "/usr/bin/codex", "exec", "brief",
        ],
    )
    monkeypatch.setattr(
        "app.lab.codex_runtime.launcher._enter_isolation",
        lambda **kwargs: entered.append(kwargs),
    )

    def fake_exec(file, argv, _env):
        executed.append((file, argv))
        raise RuntimeError("exec boundary reached")

    monkeypatch.setattr("app.lab.codex_runtime.launcher.os.execvpe", fake_exec)

    with pytest.raises(RuntimeError, match="exec boundary reached"):
        launcher_main()
    assert entered == [{
        "uid": 20001,
        "gid": 20001,
        "cgroup_path": "/cgroup/run-random",
        "cwd": "/workspace",
    }]
    assert executed == [
        ("/usr/bin/codex", ["/usr/bin/codex", "exec", "brief"])
    ]


@pytest.mark.anyio
async def test_generic_consumer_error_kills_the_entire_process_group(
    tmp_path, monkeypatch
):
    class ExplodingStdout:
        async def readline(self):
            raise ValueError("JSONL line exceeded the stream limit")

    class FakeProcess:
        pid = 43210
        returncode = None
        stdout = ExplodingStdout()
        stderr = SimpleNamespace(read=lambda: b"")

        async def wait(self):
            self.returncode = -9
            return self.returncode

    class FakeProxy:
        def __init__(self, *, gateway_token, **_kwargs):
            self.gateway_token = gateway_token

        async def start(self):
            return "http://127.0.0.1:4321/v1"

        def check_healthy(self):
            return None

        async def close(self):
            return None

    process = FakeProcess()
    spawn_kwargs = {}
    killed_groups = []

    async def fake_spawn(*_args, **kwargs):
        spawn_kwargs.update(kwargs)
        run_tmp = Path(kwargs["env"]["TMPDIR"])
        assert run_tmp.parent == workspace
        assert run_tmp.stat().st_mode & 0o777 == 0o700
        return process

    async def fake_usage(_session, _base_url):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "cost_usd_cents": 0}

    async def fake_revoke(_session, _base_url):
        return None

    def fake_killpg(pgid, signal_number):
        killed_groups.append((pgid, signal_number))

    monkeypatch.setattr(
        "app.lab.codex_runtime.service.RunCredentialProxy", FakeProxy
    )
    monkeypatch.setattr(
        "app.lab.codex_runtime.service.asyncio.create_subprocess_exec", fake_spawn
    )
    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_usage", fake_usage)
    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_revoke", fake_revoke)
    monkeypatch.setattr("app.lab.codex_runtime.service.os.killpg", fake_killpg)

    root = tmp_path / "runs"
    workspace = root / "run"
    workspace.mkdir(parents=True)
    request = CreateRun(
        run_id="generic-error", tenant_id="tenant", scopes=["code"],
        budget_usd=1.0, egress_allowlist=[], model_tier="low",
        model_name="deepseek-v4-flash", model_policy_version="test",
        resource_cpu_cores=2, resource_memory_mb=2048,
        model_gateway_base_url="http://ignored/v1",
        model_gateway_token="real-run-token",
    )
    session = RuntimeSession("generic-error", request, workspace)
    config = CodexRuntimeConfig(
        bind_host="127.0.0.1", bind_port=8097, api_key=API_KEY,
        codex_binary="/bin/false", workspace_root=str(root),
        max_active_runs=1, run_timeout_s=10, max_step_text_chars=1000,
        model_gateway_base_url="http://trusted-gateway:8096/v1",
        enforce_process_isolation=False,
    )

    await _consume_codex(
        session,
        Goal(brief="trigger parser failure", scopes=["code"]),
        config,
        RuntimeResourcePool(max_runs=1, cpu_cores=2, memory_mb=2048),
    )

    assert spawn_kwargs["start_new_session"] is True
    assert killed_groups == [(process.pid, 9)]
    assert process.returncode == -9
    assert session.failed is True


@pytest.mark.anyio
async def test_run_credential_proxy_hides_gateway_token_and_denies_control_paths():
    observed_authorization = []

    def gateway(request: httpx.Request) -> httpx.Response:
        observed_authorization.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"object": "list", "data": []})

    proxy = RunCredentialProxy(
        gateway_base_url="http://trusted-gateway/v1",
        gateway_token="real-gateway-token",
        client_token="per-run-proxy-token",
    )
    await proxy._client.aclose()
    proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(gateway))
    base_url = await proxy.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            denied = await client.get(
                base_url + "/models",
                headers={"Authorization": "Bearer another-run-token"},
            )
            assert denied.status_code == 401
            control = await client.post(
                base_url + "/lab/revoke",
                headers={"Authorization": "Bearer per-run-proxy-token"},
            )
            assert control.status_code == 403
            allowed = await client.get(
                base_url + "/models",
                headers={"Authorization": "Bearer per-run-proxy-token"},
            )
            assert allowed.status_code == 200
    finally:
        await proxy.close()
    assert observed_authorization == ["Bearer real-gateway-token"]


def test_hidepid_accepts_kernel_canonical_invisible_name(monkeypatch):
    monkeypatch.setattr(
        "app.lab.codex_runtime.service.Path.read_text",
        lambda _path, **_kwargs: "proc /proc proc rw,nosuid,hidepid=invisible 0 0\n",
    )
    assert _has_hidepid_2() is True


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
        model_gateway_base_url="http://trusted-gateway:8096/v1",
        enforce_process_isolation=False,
    )

    async def fake_usage(_session, _base_url):
        return {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                "cost_usd_cents": 1}

    async def fake_revoke(_session, _base_url):
        return None

    real_spawn = asyncio.create_subprocess_exec
    observed_stdin = []
    observed_tokens = []

    async def capture_spawn(*args, **kwargs):
        observed_stdin.append(kwargs.get("stdin"))
        observed_tokens.append(kwargs["env"].get("LAB_RUN_TOKEN"))
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_usage", fake_usage)
    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_revoke", fake_revoke)
    monkeypatch.setattr(
        "app.lab.codex_runtime.service.asyncio.create_subprocess_exec", capture_spawn
    )
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
        assert session_id == "run"
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
        assert not (Path(config.workspace_root) / session_id).exists()
        stopped = await client.post(f"/runs/{session_id}/stop", headers=headers)
        assert stopped.status_code == 200
        assert not (Path(config.workspace_root) / session_id).exists()
    assert observed_stdin == [asyncio.subprocess.DEVNULL]
    assert observed_tokens and observed_tokens[0] != "run-token"


def test_codex_shell_environment_excludes_gateway_token():
    rendered = _codex_config("http://gateway:8096/v1")
    assert 'wire_api = "responses"' in rendered
    assert 'inherit = "none"' in rendered
    assert 'exclude = ["LAB_RUN_TOKEN", "*KEY*", "*SECRET*", "*TOKEN*"]' in rendered


def test_runtime_config_requires_key_file_and_rejects_full_access(tmp_path):
    key_file = tmp_path / "runtime-key"
    key_file.write_text(API_KEY, encoding="utf-8")
    key_file.chmod(0o600)
    base = {
        "LAB_CODEX_RUNTIME_API_KEY_FILE": str(key_file),
        "LAB_CODEX_RUNTIME_MODEL_GATEWAY_BASE_URL": "http://gateway:8096/v1",
    }
    config = CodexRuntimeConfig.from_env(base)
    assert config.api_key == API_KEY
    assert config.codex_sandbox == "workspace-write"
    with pytest.raises(ValueError, match="SANDBOX is invalid"):
        CodexRuntimeConfig.from_env({
            **base,
            "LAB_CODEX_RUNTIME_SANDBOX": "danger-full-access",
        })
    with pytest.raises(ValueError, match="API_KEY_FILE is required"):
        CodexRuntimeConfig.from_env({
            "LAB_CODEX_RUNTIME_API_KEY": API_KEY,
            "LAB_CODEX_RUNTIME_MODEL_GATEWAY_BASE_URL": "http://gateway:8096/v1",
        })


@pytest.mark.anyio
async def test_usage_lookup_failure_marks_successful_process_failed(tmp_path, monkeypatch):
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    config = CodexRuntimeConfig(
        bind_host="127.0.0.1", bind_port=8097, api_key=API_KEY,
        codex_binary=str(executable), workspace_root=str(tmp_path / "runs"),
        max_active_runs=1, run_timeout_s=10, max_step_text_chars=1000,
        model_gateway_base_url="http://trusted-gateway:8096/v1",
        enforce_process_isolation=False,
    )

    async def failed_usage(_session, _base_url):
        raise RuntimeError("ledger offline")

    async def fake_revoke(_session, _base_url):
        return None

    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_usage", failed_usage)
    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_revoke", fake_revoke)
    app = create_app(config)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    body = {
        "run_id": "usage-failure", "tenant_id": "tenant", "scopes": ["code"],
        "budget_usd": 0.25, "egress_allowlist": [], "model_tier": "low",
        "model_name": "deepseek-v4-flash", "model_policy_version": "test",
        "resource_cpu_cores": 2, "resource_memory_mb": 2048,
        "model_gateway_base_url": "http://ignored/v1",
        "model_gateway_token": "real-run-token",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        session_id = (await client.post("/runs", headers=headers, json=body)).json()["session_id"]
        await client.post(
            f"/runs/{session_id}/goal", headers=headers,
            json={"brief": "finish", "scopes": ["code"]},
        )
        for _ in range(30):
            result = await client.get(f"/runs/{session_id}/steps", headers=headers)
            if result.json()["done"]:
                break
            await asyncio.sleep(0.01)
        assert result.json()["failed"] is True
        assert "model usage unavailable" in result.json()["error"]
        assert not (Path(config.workspace_root) / session_id).exists()


@pytest.mark.anyio
async def test_cancel_waits_for_usage_revoke_and_workspace_cleanup(tmp_path, monkeypatch):
    executable = tmp_path / "slow-codex"
    executable.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    config = CodexRuntimeConfig(
        bind_host="127.0.0.1", bind_port=8097, api_key=API_KEY,
        codex_binary=str(executable), workspace_root=str(tmp_path / "runs"),
        max_active_runs=1, run_timeout_s=120, max_step_text_chars=1000,
        model_gateway_base_url="http://trusted-gateway:8096/v1",
        enforce_process_isolation=False,
    )
    revoked = asyncio.Event()

    async def fake_usage(_session, _base_url):
        return {"input_tokens": 7, "output_tokens": 0, "total_tokens": 7,
                "cost_usd_cents": 1}

    async def fake_revoke(_session, _base_url):
        revoked.set()

    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_usage", fake_usage)
    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_revoke", fake_revoke)
    app = create_app(config)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    body = {
        "run_id": "cancelled-run", "tenant_id": "tenant", "scopes": ["code"],
        "budget_usd": 0.25, "egress_allowlist": [], "model_tier": "low",
        "model_name": "deepseek-v4-flash", "model_policy_version": "test",
        "resource_cpu_cores": 2, "resource_memory_mb": 2048,
        "model_gateway_base_url": "http://ignored/v1",
        "model_gateway_token": "real-run-token",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        await client.post("/runs", headers=headers, json=body)
        await client.post(
            "/runs/cancelled-run/goal", headers=headers,
            json={"brief": "wait", "scopes": ["code"]},
        )
        for _ in range(30):
            health = (await client.get("/healthz")).json()
            if health["active"] == 1:
                break
            await asyncio.sleep(0.01)
        cancelled = await client.post("/runs/cancelled-run/cancel", headers=headers)
        assert cancelled.status_code == 200
        result = (await client.get("/runs/cancelled-run/steps", headers=headers)).json()
        assert result["done"] is True
        assert result["failed"] is True
        assert "cancelled" in result["error"]
        assert revoked.is_set()
        assert list(Path(config.workspace_root).iterdir()) == []


@pytest.mark.anyio
async def test_cancel_during_isolation_setup_revokes_and_reports_cancelled(
    tmp_path, monkeypatch
):
    setup_entered = asyncio.Event()
    revoked = asyncio.Event()

    # Only start() is replaced: the teardown path must survive the real
    # check_healthy()/close() with a proxy that never finished starting.
    async def hanging_start(self):
        setup_entered.set()
        await asyncio.sleep(60)
        return "http://127.0.0.1:4321/v1"

    async def fake_usage(_session, _base_url):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "cost_usd_cents": 0}

    async def fake_revoke(_session, _base_url):
        revoked.set()

    monkeypatch.setattr(RunCredentialProxy, "start", hanging_start)
    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_usage", fake_usage)
    monkeypatch.setattr("app.lab.codex_runtime.service._gateway_revoke", fake_revoke)
    config = CodexRuntimeConfig(
        bind_host="127.0.0.1", bind_port=8097, api_key=API_KEY,
        codex_binary="/bin/false", workspace_root=str(tmp_path / "runs"),
        max_active_runs=1, run_timeout_s=120, max_step_text_chars=1000,
        model_gateway_base_url="http://trusted-gateway:8096/v1",
        enforce_process_isolation=False,
    )
    app = create_app(config)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    body = {
        "run_id": "setup-cancel", "tenant_id": "tenant", "scopes": ["code"],
        "budget_usd": 0.25, "egress_allowlist": [], "model_tier": "low",
        "model_name": "deepseek-v4-flash", "model_policy_version": "test",
        "resource_cpu_cores": 2, "resource_memory_mb": 2048,
        "model_gateway_base_url": "http://ignored/v1",
        "model_gateway_token": "real-run-token",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        await client.post("/runs", headers=headers, json=body)
        await client.post(
            "/runs/setup-cancel/goal", headers=headers,
            json={"brief": "wait", "scopes": ["code"]},
        )
        await asyncio.wait_for(setup_entered.wait(), timeout=5)
        cancelled = await client.post("/runs/setup-cancel/cancel", headers=headers)
        assert cancelled.status_code == 200
        result = (await client.get("/runs/setup-cancel/steps", headers=headers)).json()
        assert result["done"] is True
        assert result["failed"] is True
        assert "cancelled" in result["error"]
        assert revoked.is_set()
        assert list(Path(config.workspace_root).iterdir()) == []


@pytest.mark.anyio
async def test_codex_runtime_rejects_non_code_scope(tmp_path):
    config = CodexRuntimeConfig(
        bind_host="127.0.0.1", bind_port=8097, api_key=API_KEY,
        codex_binary="/bin/false", workspace_root=str(tmp_path / "runs"),
        max_active_runs=1, run_timeout_s=10, max_step_text_chars=1000,
        model_gateway_base_url="http://trusted-gateway:8096/v1",
        enforce_process_isolation=False,
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
        model_gateway_base_url="http://trusted-gateway:8096/v1",
        enforce_process_isolation=False,
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
