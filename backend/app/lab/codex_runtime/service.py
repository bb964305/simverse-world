from __future__ import annotations

import asyncio
import hmac
import json
import os
import shutil
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.lab.codex_runtime.config import CodexRuntimeConfig
from app.lab.model_catalog import FLASH_MODEL, PRO_MODEL, RESOURCE_PROFILES


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
        'include_only = ["PATH", "HOME", "LANG"]\n'
        'exclude = ["LAB_RUN_TOKEN", "*KEY*", "*SECRET*", "*TOKEN*"]\n'
    )


async def _gateway_usage(session: RuntimeSession) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(
                session.request.model_gateway_base_url.rstrip("/") + "/lab/usage",
                headers={"Authorization": f"Bearer {session.request.model_gateway_token}"},
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
        (codex_home / "config.toml").write_text(
            _codex_config(session.request.model_gateway_base_url), encoding="utf-8"
        )
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
            "LAB_RUN_TOKEN": session.request.model_gateway_token,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        command = [
            sys.executable,
            "-m", "app.lab.codex_runtime.launcher",
            "--cpu-cores", str(session.request.resource_cpu_cores),
            "--memory-mb", str(session.request.resource_memory_mb),
            "--",
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
        try:
            session.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=session.workspace,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
            )
            assert session.process.stdout is not None
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
            if return_code != 0:
                stderr = ""
                if session.process.stderr is not None:
                    stderr = _safe_text(await session.process.stderr.read(), 2000)
                raise RuntimeError(f"Codex exited with status {return_code}: {stderr}")
            if not session.final_text:
                raise RuntimeError("Codex completed without a final report")
            usage = await _gateway_usage(session)
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
            await session.finish()
        except asyncio.CancelledError:
            if session.process and session.process.returncode is None:
                session.process.kill()
                await session.process.wait()
            await session.finish(failed=True, error="Codex run cancelled")
            raise
        except TimeoutError:
            if session.process and session.process.returncode is None:
                session.process.kill()
                await session.process.wait()
            await session.finish(failed=True, error="Codex run timed out")
        except Exception as exc:
            await session.finish(failed=True, error=str(exc)[:1000])


def create_app(config: CodexRuntimeConfig) -> FastAPI:
    app = FastAPI(title="Simverse Codex Runtime", docs_url=None, redoc_url=None)
    sessions: dict[str, RuntimeSession] = {}
    resource_pool = RuntimeResourcePool(
        max_runs=config.max_active_runs,
        cpu_cores=config.total_cpu_cores,
        memory_mb=config.total_memory_mb,
    )
    root = Path(config.workspace_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "active": resource_pool.active_runs,
            "queued": max(0, sum(not item.done for item in sessions.values()) - resource_pool.active_runs),
            "used_cpu_cores": resource_pool.used_cpu_cores,
            "used_memory_mb": resource_pool.used_memory_mb,
        }

    @app.post("/runs")
    async def create_run(
        body: CreateRun, authorization: str | None = Header(default=None)
    ) -> dict:
        _auth(authorization, config)
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
        session_id = str(uuid.uuid4())
        workspace = root / session_id
        workspace.mkdir(mode=0o700)
        session = RuntimeSession(session_id, body, workspace)
        sessions[session_id] = session
        return {"session_id": session_id}

    def get_session(session_id: str) -> RuntimeSession:
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
            _consume_codex(session, body, config, resource_pool)
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
            session.process.kill() if kill else session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=5)
            except TimeoutError:
                session.process.kill()
                await session.process.wait()

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
            await stop_process(session, kill=action == "kill")
            if session.task and not session.task.done():
                session.task.cancel()
        if action == "stop" and session.done:
            resolved_root = root.resolve()
            resolved_workspace = session.workspace.resolve()
            if resolved_workspace.parent == resolved_root and resolved_workspace.exists():
                shutil.rmtree(resolved_workspace)
        return {"stopped": True}

    @app.get("/runs/{session_id}/health")
    async def run_health(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        _auth(authorization, config)
        session = get_session(session_id)
        return {"alive": not session.done, "cancelled": session.failed and "cancel" in session.error.lower()}

    return app
