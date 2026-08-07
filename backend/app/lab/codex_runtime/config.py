from __future__ import annotations

import os
import stat
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
    model_gateway_base_url: str
    codex_sandbox: str = "workspace-write"
    total_cpu_cores: int = 4
    total_memory_mb: int = 8192
    enforce_process_isolation: bool = True
    cgroup_root: str = "/sys/fs/cgroup/simverse-lab"
    session_ttl_s: int = 3600
    max_sessions: int = 128
    usage_poll_s: float = 5.0

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> "CodexRuntimeConfig":
        env = os.environ if environ is None else environ
        api_key_file = _required(env, "LAB_CODEX_RUNTIME_API_KEY_FILE")
        api_key_path = Path(api_key_file)
        if not api_key_path.is_absolute():
            raise ValueError("LAB_CODEX_RUNTIME_API_KEY_FILE must be absolute")
        try:
            key_stat = api_key_path.stat()
            if not stat.S_ISREG(key_stat.st_mode) or key_stat.st_mode & 0o077:
                raise ValueError(
                    "LAB_CODEX_RUNTIME_API_KEY_FILE must be a private regular file"
                )
            api_key = api_key_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("LAB_CODEX_RUNTIME_API_KEY_FILE is unreadable") from exc
        if len(api_key.encode("utf-8")) < 32:
            raise ValueError("runtime API key file must contain at least 32 bytes")
        try:
            port = int(env.get("LAB_CODEX_RUNTIME_BIND_PORT", "8097"))
            max_runs = int(env.get("LAB_CODEX_RUNTIME_MAX_ACTIVE_RUNS", "2"))
            timeout = int(env.get("LAB_CODEX_RUNTIME_RUN_TIMEOUT_S", "1200"))
            max_chars = int(env.get("LAB_CODEX_RUNTIME_MAX_STEP_TEXT_CHARS", "12000"))
            total_cpu = int(env.get("LAB_CODEX_RUNTIME_TOTAL_CPU_CORES", "4"))
            total_memory = int(env.get("LAB_CODEX_RUNTIME_TOTAL_MEMORY_MB", "8192"))
            session_ttl = int(env.get("LAB_CODEX_RUNTIME_SESSION_TTL_S", "3600"))
            max_sessions = int(env.get("LAB_CODEX_RUNTIME_MAX_SESSIONS", "128"))
            usage_poll = float(env.get("LAB_CODEX_RUNTIME_USAGE_POLL_S", "5"))
        except ValueError as exc:
            raise ValueError("Codex runtime numeric configuration is invalid") from exc
        if not 1 <= port <= 65535 or min(
            max_runs, timeout, max_chars, total_cpu, total_memory,
            session_ttl, max_sessions,
        ) <= 0 or usage_poll <= 0:
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
        if codex_sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("LAB_CODEX_RUNTIME_SANDBOX is invalid")
        model_gateway_base_url = _required(
            env, "LAB_CODEX_RUNTIME_MODEL_GATEWAY_BASE_URL"
        ).rstrip("/")
        if not model_gateway_base_url.startswith(("http://", "https://")):
            raise ValueError(
                "LAB_CODEX_RUNTIME_MODEL_GATEWAY_BASE_URL must be an HTTP URL"
            )
        cgroup_root = env.get(
            "LAB_CODEX_RUNTIME_CGROUP_ROOT", "/sys/fs/cgroup/simverse-lab"
        )
        if not Path(cgroup_root).is_absolute():
            raise ValueError("LAB_CODEX_RUNTIME_CGROUP_ROOT must be absolute")
        return cls(
            bind_host=env.get("LAB_CODEX_RUNTIME_BIND_HOST", "0.0.0.0"),
            bind_port=port,
            api_key=api_key,
            codex_binary=codex_binary,
            workspace_root=workspace_root,
            max_active_runs=max_runs,
            run_timeout_s=timeout,
            max_step_text_chars=max_chars,
            model_gateway_base_url=model_gateway_base_url,
            codex_sandbox=codex_sandbox,
            total_cpu_cores=total_cpu,
            total_memory_mb=total_memory,
            enforce_process_isolation=True,
            cgroup_root=cgroup_root,
            session_ttl_s=session_ttl,
            max_sessions=max_sessions,
            usage_poll_s=usage_poll,
        )
