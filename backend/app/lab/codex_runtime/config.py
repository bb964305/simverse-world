from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value or value != value.strip():
        raise ValueError(f"{name} is required and must be canonical")
    return value


@dataclass(frozen=True)
class CodexRuntimeConfig:
    bind_host: str
    bind_port: int
    api_key: str
    codex_binary: str
    workspace_root: str
    max_active_runs: int
    run_timeout_s: int
    max_step_text_chars: int
    codex_sandbox: str = "workspace-write"
    total_cpu_cores: int = 4
    total_memory_mb: int = 8192

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> "CodexRuntimeConfig":
        env = os.environ if environ is None else environ
        api_key = _required(env, "LAB_CODEX_RUNTIME_API_KEY")
        if len(api_key.encode("utf-8")) < 32:
            raise ValueError("LAB_CODEX_RUNTIME_API_KEY must be at least 32 bytes")
        try:
            port = int(env.get("LAB_CODEX_RUNTIME_BIND_PORT", "8097"))
            max_runs = int(env.get("LAB_CODEX_RUNTIME_MAX_ACTIVE_RUNS", "2"))
            timeout = int(env.get("LAB_CODEX_RUNTIME_RUN_TIMEOUT_S", "1200"))
            max_chars = int(env.get("LAB_CODEX_RUNTIME_MAX_STEP_TEXT_CHARS", "12000"))
            total_cpu = int(env.get("LAB_CODEX_RUNTIME_TOTAL_CPU_CORES", "4"))
            total_memory = int(env.get("LAB_CODEX_RUNTIME_TOTAL_MEMORY_MB", "8192"))
        except ValueError as exc:
            raise ValueError("Codex runtime numeric configuration is invalid") from exc
        if not 1 <= port <= 65535 or min(
            max_runs, timeout, max_chars, total_cpu, total_memory
        ) <= 0:
            raise ValueError("Codex runtime numeric configuration is out of range")
        if total_cpu < 4 or total_memory < 4096:
            raise ValueError("Codex runtime capacity cannot admit the high resource tier")
        codex_binary = env.get("LAB_CODEX_RUNTIME_BINARY", "/usr/local/bin/codex")
        workspace_root = env.get(
            "LAB_CODEX_RUNTIME_WORKSPACE_ROOT", "/var/lib/simverse/codex-runs"
        )
        if not Path(codex_binary).is_absolute() or not Path(workspace_root).is_absolute():
            raise ValueError("Codex runtime paths must be absolute")
        codex_sandbox = env.get("LAB_CODEX_RUNTIME_SANDBOX", "workspace-write")
        if codex_sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("LAB_CODEX_RUNTIME_SANDBOX is invalid")
        return cls(
            bind_host=env.get("LAB_CODEX_RUNTIME_BIND_HOST", "0.0.0.0"),
            bind_port=port,
            api_key=api_key,
            codex_binary=codex_binary,
            workspace_root=workspace_root,
            max_active_runs=max_runs,
            run_timeout_s=timeout,
            max_step_text_chars=max_chars,
            codex_sandbox=codex_sandbox,
            total_cpu_cores=total_cpu,
            total_memory_mb=total_memory,
        )
