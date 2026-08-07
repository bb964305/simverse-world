from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.lab.codex_runtime.config import CodexRuntimeConfig
from app.lab.codex_runtime.credential_proxy import RunCredentialProxy
from app.lab import guard
from app.lab.model_catalog import FLASH_MODEL, PRO_MODEL, RESOURCE_PROFILES


logger = logging.getLogger(__name__)


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    scopes: list[str]
    budget_usd: float = Field(gt=0)
    egress_allowlist: list[str]
    model_tier: str
    model_name: str
    model_policy_version: str = Field(min_length=1, max_length=100)
    resource_cpu_cores: int = Field(gt=0)
    resource_memory_mb: int = Field(gt=0)
    model_gateway_base_url: str = Field(min_length=1, max_length=2048)
    model_gateway_token: str = Field(min_length=1, max_length=16384)


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    brief: str = Field(min_length=1, max_length=100_000)
    scopes: list[str]


@dataclass
class RuntimeSession:
    session_id: str
    request: CreateRun
    workspace: Path
    steps: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    failed: bool = False
    error: str = ""
    final_text: str = ""
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    executor_uid: int = 0
    finished_at: float | None = None

    async def append(self, **step: Any) -> None:
        async with self.condition:
            step["seq"] = len(self.steps) + 1
            self.steps.append(step)
            self.condition.notify_all()

    async def finish(self, *, failed: bool = False, error: str = "") -> None:
        async with self.condition:
            self.done = True
            self.failed = failed
            self.error = error
            self.finished_at = time.monotonic()
            self.condition.notify_all()


class RuntimeResourcePool:
    def __init__(self, *, max_runs: int, cpu_cores: int, memory_mb: int) -> None:
        self.max_runs = max_runs
        self.cpu_cores = cpu_cores
        self.memory_mb = memory_mb
        self.active_runs = 0
        self.used_cpu_cores = 0
        self.used_memory_mb = 0
        self.condition = asyncio.Condition()

    @asynccontextmanager
    async def allocation(self, *, cpu_cores: int, memory_mb: int):
        async with self.condition:
            await self.condition.wait_for(lambda: (
                self.active_runs < self.max_runs
                and self.used_cpu_cores + cpu_cores <= self.cpu_cores
                and self.used_memory_mb + memory_mb <= self.memory_mb
            ))
            self.active_runs += 1
            self.used_cpu_cores += cpu_cores
            self.used_memory_mb += memory_mb
        try:
            yield
        finally:
            async with self.condition:
                self.active_runs -= 1
                self.used_cpu_cores -= cpu_cores
                self.used_memory_mb -= memory_mb
                self.condition.notify_all()

def _auth(authorization: str | None, config: CodexRuntimeConfig) -> None:
    expected = f"Bearer {config.api_key}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def _safe_text(value: Any, limit: int) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def _codex_config(base_url: str) -> str:
    escaped = base_url.replace("\\", "\\\\").replace('"', '\\"')
    return (
        'model = "lab-auto"\n'
        'model_provider = "lab_gateway"\n\n'
        'web_search = "disabled"\n\n'
        '[model_providers.lab_gateway]\n'
        'name = "Simverse Lab model gateway"\n'
        f'base_url = "{escaped}"\n'
        'env_key = "LAB_RUN_TOKEN"\n'
        'wire_api = "responses"\n'
        '\n[shell_environment_policy]\n'
        'inherit = "none"\n'
        'ignore_default_excludes = false\n'
        'include_only = ["PATH", "HOME", "TMPDIR", "LANG"]\n'
        'exclude = ["LAB_RUN_TOKEN", "*KEY*", "*SECRET*", "*TOKEN*"]\n'
    )


async def _gateway_usage(session: RuntimeSession, base_url: str) -> dict:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.get(
            base_url.rstrip("/") + "/lab/usage",
            headers={"Authorization": f"Bearer {session.request.model_gateway_token}"},
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("cost_unknown") is True:
            raise RuntimeError("model gateway usage is not trustworthy")
        return data


async def _gateway_revoke(session: RuntimeSession, base_url: str) -> None:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(
            base_url.rstrip("/") + "/lab/revoke",
            headers={"Authorization": f"Bearer {session.request.model_gateway_token}"},
        )
        response.raise_for_status()


def _has_hidepid_2() -> bool:
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(
        fields[1] == "/proc"
        and {"hidepid=2", "hidepid=invisible"}.intersection(fields[3].split(","))
        for line in mounts.splitlines()
        if len(fields := line.split()) >= 4
    )


def _validate_process_isolation(config: CodexRuntimeConfig) -> None:
    if not config.enforce_process_isolation:
        return
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("isolated Codex Runtime requires a root Linux controller")
    if not _has_hidepid_2():
        raise RuntimeError("isolated Codex Runtime requires /proc hidepid=2")
    root = Path(config.cgroup_root)
    controllers = root / "cgroup.controllers"
    if not root.is_dir() or not os.access(root, os.W_OK) or not controllers.is_file():
        raise RuntimeError("isolated Codex Runtime requires a delegated cgroup v2 root")
    available = set(controllers.read_text(encoding="ascii").split())
    if not {"cpu", "memory", "pids"}.issubset(available):
        raise RuntimeError("Codex cgroup delegation lacks cpu, memory, or pids")


def _create_run_cgroup(session: RuntimeSession, config: CodexRuntimeConfig) -> Path | None:
    if not config.enforce_process_isolation:
        return None
    path = Path(config.cgroup_root) / f"run-{secrets.token_hex(16)}"
    path.mkdir(mode=0o700)
    (path / "cpu.max").write_text(
        f"{session.request.resource_cpu_cores * 100_000} 100000", encoding="ascii"
    )
    (path / "memory.max").write_text(
        str(session.request.resource_memory_mb * 1024 * 1024), encoding="ascii"
    )
    (path / "pids.max").write_text("256", encoding="ascii")
    return path


def _remove_workspace(
    workspace: Path, workspace_root: str, *, restore_controller_owner: bool
) -> None:
    resolved_root = Path(workspace_root).resolve()
    resolved_workspace = workspace.resolve()
    if resolved_workspace.parent != resolved_root or not resolved_workspace.exists():
        return
    if restore_controller_owner:
        def restore(path: Path, *, directory: bool) -> None:
            try:
                os.chown(path, 0, 0, follow_symlinks=False)
            except OSError:
                logger.warning(
                    "could not restore controller ownership during workspace cleanup",
                    exc_info=True,
                )
            if directory and not path.is_symlink():
                try:
                    path.chmod(0o700)
                except OSError:
                    logger.warning(
                        "could not restore directory mode during workspace cleanup",
                        exc_info=True,
                    )

        restore(resolved_workspace, directory=True)
        for current, directories, _files in os.walk(
            resolved_workspace,
            topdown=True,
            followlinks=False,
            onerror=lambda exc: logger.warning(
                "could not enumerate part of a run workspace during cleanup",
                exc_info=(type(exc), exc, exc.__traceback__),
            ),
        ):
            current_path = Path(current)
            restore(current_path, directory=True)
            for name in directories:
                restore(current_path / name, directory=True)

    # A run can race cleanup by tightening a directory mode. Restore modes and
    # retry the complete tree rather than allowing one failed entry to strand it.
    last_error: OSError | None = None
    for _attempt in range(3):
        failures: list[OSError] = []

        def recover_remove(_function: Any, path: str, exc: OSError) -> None:
            failures.append(exc)
            candidate = Path(path)
            try:
                if not candidate.is_symlink():
                    candidate.chmod(0o700)
            except OSError:
                logger.warning(
                    "could not restore mode while retrying workspace removal",
                    exc_info=True,
                )

        if sys.version_info >= (3, 12):
            shutil.rmtree(resolved_workspace, onexc=recover_remove)
        else:
            shutil.rmtree(
                resolved_workspace,
                onerror=lambda function, path, exc_info: recover_remove(
                    function, path, exc_info[1]
                ),
            )
        if not resolved_workspace.exists():
            return
        if failures:
            last_error = failures[-1]
    if last_error is not None:
        raise last_error
    raise OSError(f"workspace cleanup left residue at {resolved_workspace}")


async def _terminate_process_group(
    process: asyncio.subprocess.Process | None, *, sig: signal.Signals = signal.SIGKILL
) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except OSError:
        logger.exception("failed to signal Codex process group %s", process.pid)
    if sig == signal.SIGKILL:
        # Do not release the resource-pool slot or start workspace cleanup until
        # the kernel has reaped the full run session.
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        logger.warning(
            "Codex process group %s ignored graceful termination", process.pid
        )


async def _monitor_budget(
    session: RuntimeSession,
    config: CodexRuntimeConfig,
    credential_proxy: RunCredentialProxy,
) -> str:
    try:
        while True:
            await asyncio.sleep(config.usage_poll_s)
            credential_proxy.check_healthy()
            session.request.model_gateway_token = credential_proxy.gateway_token
            usage = await _gateway_usage(session, config.model_gateway_base_url)
            cost_cents = int(usage.get("cost_usd_cents") or 0)
            if guard.check_budget(
                cost_cents, int(session.request.budget_usd * 100)
            ):
                continue
            await _terminate_process_group(session.process)
            return "model budget exceeded"
    except asyncio.CancelledError:
        raise
    except Exception:
        await _terminate_process_group(session.process)
        return "model usage unavailable"


async def _consume_codex(
    session: RuntimeSession,
    goal: Goal,
    config: CodexRuntimeConfig,
    resource_pool: RuntimeResourcePool,
) -> None:
    async with resource_pool.allocation(
        cpu_cores=session.request.resource_cpu_cores,
        memory_mb=session.request.resource_memory_mb,
    ):
        codex_home = session.workspace / ".codex"
        codex_home.mkdir(mode=0o700)
        run_tmp = session.workspace / ".tmp"
        run_tmp.mkdir(mode=0o700)
        client_token = secrets.token_urlsafe(32)
        credential_proxy = RunCredentialProxy(
            gateway_base_url=config.model_gateway_base_url,
            gateway_token=session.request.model_gateway_token,
            client_token=client_token,
        )
        cgroup_path = None
        terminal_error = ""
        failed = False
        cancelled = False
        budget_monitor: asyncio.Task[str] | None = None
        try:
            # A cancel landing anywhere in isolation setup must still reach the
            # shared finally below: revoke the gateway run token, close the
            # proxy, and remove the workspace. The finally tolerates a
            # partially built run (process is None, proxy never started,
            # cgroup_path is None).
            try:
                proxy_base_url = await credential_proxy.start()
                config_path = codex_home / "config.toml"
                config_path.write_text(_codex_config(proxy_base_url), encoding="utf-8")
                cgroup_path = _create_run_cgroup(session, config)
                if config.enforce_process_isolation:
                    os.chown(config_path, session.executor_uid, session.executor_uid)
                    os.chown(codex_home, session.executor_uid, session.executor_uid)
                    os.chown(run_tmp, session.executor_uid, session.executor_uid)
                    os.chown(session.workspace, session.executor_uid, session.executor_uid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise RuntimeError(f"runtime isolation setup failed: {exc}") from exc
            prompt = (
                "You are the assigned Simverse Lab researcher. Complete the task inside "
                "the provided workspace. Use shell/file tools only when needed. Do not "
                "attempt network access or financial actions. End with a concise report "
                "that states what you did and the result.\n\nTask:\n" + goal.brief
            )
            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(session.workspace),
                "CODEX_HOME": str(codex_home),
                "TMPDIR": str(run_tmp),
                # This authenticates only to the per-run loopback proxy. It is not a
                # gateway credential and cannot be used outside this run.
                "LAB_RUN_TOKEN": client_token,
                "LANG": os.environ.get("LANG", "C.UTF-8"),
            }
            for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
                if os.environ.get(name):
                    env[name] = os.environ[name]
            codex_command = [
                config.codex_binary,
                "--ask-for-approval", "never",
                "exec",
                "--ephemeral",
                "--json",
                "--sandbox", config.codex_sandbox,
                "--skip-git-repo-check",
                "--model", "lab-auto",
                prompt,
            ]
            command = codex_command
            process_cwd: Path | None = session.workspace
            if config.enforce_process_isolation:
                assert cgroup_path is not None
                command = [
                    sys.executable,
                    "-m", "app.lab.codex_runtime.launcher",
                    "--uid", str(session.executor_uid),
                    "--gid", str(session.executor_uid),
                    "--cgroup-path", str(cgroup_path),
                    "--cwd", str(session.workspace),
                    "--",
                    *codex_command,
                ]
                process_cwd = None
            session.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=process_cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
                start_new_session=True,
            )
            assert session.process.stdout is not None
            budget_monitor = asyncio.create_task(
                _monitor_budget(session, config, credential_proxy)
            )
            async with asyncio.timeout(config.run_timeout_s):
                while line := await session.process.stdout.readline():
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type")
                    item = event.get("item") if isinstance(event.get("item"), dict) else {}
                    item_type = item.get("type")
                    if event_type == "error":
                        terminal_error = _safe_text(
                            event.get("message", "Codex reported an error"), 2000
                        )
                    elif event_type == "turn.failed":
                        raw_error = event.get("error")
                        terminal_error = _safe_text(
                            raw_error.get("message")
                            if isinstance(raw_error, dict)
                            else raw_error or "Codex turn failed",
                            2000,
                        )
                    if event_type == "item.started" and item_type == "command_execution":
                        await session.append(
                            phase="tool_call",
                            tool="shell.exec",
                            summary=_safe_text(item.get("command", "shell command"), config.max_step_text_chars),
                            payload={"command": _safe_text(item.get("command", ""), config.max_step_text_chars)},
                        )
                    elif event_type == "item.completed" and item_type == "command_execution":
                        output = _safe_text(item.get("aggregated_output", ""), config.max_step_text_chars)
                        await session.append(
                            phase="observation",
                            tool="shell.exec",
                            summary=output or "command completed",
                            payload={"exit_code": item.get("exit_code"), "output": output},
                        )
                    elif event_type == "item.completed" and item_type == "agent_message":
                        text = _safe_text(item.get("text", ""), config.max_step_text_chars)
                        if text:
                            session.final_text = text
                            await session.append(phase="message", tool=None, summary=text, payload={})
                return_code = await session.process.wait()
            if budget_monitor.done():
                monitor_error = budget_monitor.result()
                if monitor_error:
                    raise RuntimeError(monitor_error)
            if return_code != 0:
                stderr = ""
                if session.process.stderr is not None:
                    stderr = _safe_text(await session.process.stderr.read(), 2000)
                detail = terminal_error or stderr
                raise RuntimeError(f"Codex exited with status {return_code}: {detail}")
            if not session.final_text:
                raise RuntimeError("Codex completed without a final report")
        except asyncio.CancelledError:
            cancelled = True
            failed = True
            terminal_error = "Codex run cancelled"
        except TimeoutError:
            failed = True
            terminal_error = "Codex run timed out"
        except Exception as exc:
            failed = True
            terminal_error = str(exc)[:1000]
        finally:
            await _terminate_process_group(session.process)
            if budget_monitor is not None and not budget_monitor.done():
                budget_monitor.cancel()
                await asyncio.gather(budget_monitor, return_exceptions=True)
            try:
                credential_proxy.check_healthy()
                session.request.model_gateway_token = credential_proxy.gateway_token
                usage = await _gateway_usage(session, config.model_gateway_base_url)
                await session.append(
                    phase="message",
                    tool=None,
                    summary="Codex model usage recorded",
                    payload={
                        "model": session.request.model_name,
                        "model_tier": session.request.model_tier,
                        "input_tokens": int(usage.get("input_tokens") or 0),
                        "output_tokens": int(usage.get("output_tokens") or 0),
                        "resource_cpu_cores": session.request.resource_cpu_cores,
                        "resource_memory_mb": session.request.resource_memory_mb,
                    },
                    model_tokens=int(usage.get("total_tokens") or 0),
                    cost_usd_cents=int(usage.get("cost_usd_cents") or 0),
                )
            except Exception as exc:
                failed = True
                terminal_error = f"model usage unavailable: {exc}"[:1000]
            try:
                await _gateway_revoke(session, config.model_gateway_base_url)
            except Exception as exc:
                failed = True
                terminal_error = f"model token revoke failed: {exc}"[:1000]
            session.request.model_gateway_token = ""
            await credential_proxy.close()
            try:
                _remove_workspace(
                    session.workspace,
                    config.workspace_root,
                    restore_controller_owner=config.enforce_process_isolation,
                )
            except OSError:
                logger.critical(
                    "run workspace cleanup failed for session %s",
                    session.session_id,
                    exc_info=True,
                )
                failed = True
                terminal_error = "run workspace cleanup failed"
            if cgroup_path is not None:
                try:
                    cgroup_path.rmdir()
                except OSError:
                    logger.critical(
                        "run cgroup cleanup failed for session %s",
                        session.session_id,
                        exc_info=True,
                    )
                    failed = True
                    terminal_error = "run cgroup cleanup failed"
            await session.finish(failed=failed, error=terminal_error)
            if cancelled:
                return


def create_app(config: CodexRuntimeConfig) -> FastAPI:
    _validate_process_isolation(config)
    app = FastAPI(title="Simverse Codex Runtime", docs_url=None, redoc_url=None)
    sessions: dict[str, RuntimeSession] = {}
    resource_pool = RuntimeResourcePool(
        max_runs=config.max_active_runs,
        cpu_cores=config.total_cpu_cores,
        memory_mb=config.total_memory_mb,
    )
    root = Path(config.workspace_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if config.enforce_process_isolation:
        # Dedicated run UIDs need to traverse the root, but cannot list it or
        # enter another UID's mode-0700 workspace.
        root.chmod(0o711)
    next_executor_uid = 20_000

    def prune_sessions() -> None:
        now = time.monotonic()
        stale = [
            key for key, value in sessions.items()
            if value.done and value.finished_at is not None
            and now - value.finished_at >= config.session_ttl_s
        ]
        for key in stale:
            sessions.pop(key, None)

    async def consume_safely(session: RuntimeSession, goal: Goal) -> None:
        try:
            await _consume_codex(session, goal, config, resource_pool)
        except BaseException as exc:
            try:
                _remove_workspace(
                    session.workspace,
                    config.workspace_root,
                    restore_controller_owner=config.enforce_process_isolation,
                )
            except OSError:
                logger.critical(
                    "emergency workspace cleanup failed for session %s",
                    session.session_id,
                    exc_info=True,
                )
            if not session.done:
                await session.finish(
                    failed=True,
                    error=f"runtime isolation setup failed: {exc}"[:1000],
                )

    @app.get("/healthz")
    async def healthz() -> dict:
        prune_sessions()
        return {
            "status": "ok",
            "active": resource_pool.active_runs,
            "queued": max(0, sum(not item.done for item in sessions.values()) - resource_pool.active_runs),
            "used_cpu_cores": resource_pool.used_cpu_cores,
            "used_memory_mb": resource_pool.used_memory_mb,
            "capacity_cpu_cores": resource_pool.cpu_cores,
            "capacity_memory_mb": resource_pool.memory_mb,
            "pending_profiles": [
                {
                    "cpu_cores": item.request.resource_cpu_cores,
                    "memory_mb": item.request.resource_memory_mb,
                    "task_done": bool(item.task and item.task.done()),
                }
                for item in sessions.values()
                if not item.done
            ],
        }

    @app.post("/runs")
    async def create_run(
        body: CreateRun, authorization: str | None = Header(default=None)
    ) -> dict:
        nonlocal next_executor_uid
        _auth(authorization, config)
        prune_sessions()
        if len(sessions) >= config.max_sessions:
            raise HTTPException(status_code=503, detail="runtime session capacity exhausted")
        expected = {"low": FLASH_MODEL, "high": PRO_MODEL}
        if expected.get(body.model_tier) != body.model_name:
            raise HTTPException(status_code=400, detail="model tier mismatch")
        profile = RESOURCE_PROFILES.get(body.model_tier)
        if profile is None or (
            body.resource_cpu_cores != profile["cpu_cores"]
            or body.resource_memory_mb != profile["memory_mb"]
        ):
            raise HTTPException(status_code=400, detail="resource tier mismatch")
        if "code" not in body.scopes:
            raise HTTPException(status_code=403, detail="Codex runtime requires code scope")
        if any(not isinstance(scope, str) or not scope for scope in body.scopes):
            raise HTTPException(status_code=400, detail="invalid scopes")
        session_id = body.run_id
        if session_id in sessions:
            raise HTTPException(status_code=409, detail="run already exists")
        workspace = root / str(uuid.uuid5(uuid.NAMESPACE_URL, f"simverse:codex:{session_id}"))
        workspace.mkdir(mode=0o700)
        executor_uid = next_executor_uid
        next_executor_uid += 1
        session = RuntimeSession(
            session_id, body, workspace, executor_uid=executor_uid
        )
        sessions[session_id] = session
        return {"session_id": session_id}

    def get_session(session_id: str) -> RuntimeSession:
        prune_sessions()
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="run not found")
        return session

    @app.post("/runs/{session_id}/goal")
    async def submit_goal(
        session_id: str, body: Goal, authorization: str | None = Header(default=None)
    ) -> dict:
        _auth(authorization, config)
        session = get_session(session_id)
        if body.scopes != session.request.scopes:
            raise HTTPException(status_code=409, detail="scope mismatch")
        if session.task is not None:
            raise HTTPException(status_code=409, detail="goal already submitted")
        session.task = asyncio.create_task(
            consume_safely(session, body)
        )
        return {"accepted": True}

    @app.get("/runs/{session_id}/steps")
    async def steps(
        session_id: str,
        after: int = Query(default=0, ge=0),
        authorization: str | None = Header(default=None),
    ) -> dict:
        _auth(authorization, config)
        session = get_session(session_id)
        async with session.condition:
            if len(session.steps) <= after and not session.done:
                try:
                    await asyncio.wait_for(session.condition.wait(), timeout=1.0)
                except TimeoutError:
                    pass
            return {
                "steps": session.steps[after:],
                "done": session.done,
                "failed": session.failed,
                "error": session.error if session.failed else None,
            }

    @app.get("/runs/{session_id}/artifacts")
    async def artifacts(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        _auth(authorization, config)
        session = get_session(session_id)
        if not session.done or session.failed:
            raise HTTPException(status_code=409, detail="run has no releasable artifact")
        return {"artifacts": [{
            "kind": "text",
            "title": "Codex research report",
            "text_md": session.final_text,
            "meta": {
                "model": session.request.model_name,
                "model_tier": session.request.model_tier,
                "policy_version": session.request.model_policy_version,
                "resource_cpu_cores": session.request.resource_cpu_cores,
                "resource_memory_mb": session.request.resource_memory_mb,
            },
        }]}

    async def stop_process(session: RuntimeSession, *, kill: bool) -> None:
        if session.process and session.process.returncode is None:
            await _terminate_process_group(
                session.process,
                sig=signal.SIGKILL if kill else signal.SIGTERM,
            )
            if session.process.returncode is None:
                await _terminate_process_group(session.process)

    @app.post("/runs/{session_id}/approve")
    async def approve(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        _auth(authorization, config)
        get_session(session_id)
        return {"accepted": False}

    @app.post("/runs/{session_id}/{action}")
    async def control(
        session_id: str,
        action: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        _auth(authorization, config)
        if action not in {"stop", "cancel", "terminate", "kill"}:
            raise HTTPException(status_code=404, detail="unknown action")
        session = get_session(session_id)
        if action in {"cancel", "terminate", "kill"}:
            if session.task and not session.task.done():
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
            else:
                await stop_process(session, kill=action == "kill")
        if action == "stop" and session.done:
            sessions.pop(session_id, None)
        return {"stopped": True}

    @app.get("/runs/{session_id}/health")
    async def run_health(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        _auth(authorization, config)
        session = get_session(session_id)
        return {"alive": not session.done, "cancelled": session.failed and "cancel" in session.error.lower()}

    return app
