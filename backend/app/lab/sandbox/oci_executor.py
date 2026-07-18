"""Rootless OCI sandbox executor (PRD §Data Plane, V11, P2 exit "rootless
filesystem/process/secret/network/quota/teardown/Broker-only boundary").

Runs an R1 code/shell tool intent inside a throwaway, locked-down container and
hands the Broker a clean result dict — never a container handle. Every effect a
runtime asks for still passes through the Broker; the Broker is the ONLY caller
of ``as_broker_executor`` (runtime/orchestrator never touch a container).

Isolation is expressed entirely as ``docker run`` flags (built by
``build_run_argv``, which is pure and unit-tested without a daemon): no network,
read-only rootfs, a size-quota'd ``/scratch`` tmpfs, all capabilities dropped, a
non-root user, ``no-new-privileges``, and memory/cpu/pids caps. There is never a
host bind mount and never the docker socket, so a compromised payload cannot
read host files or reach the daemon. The wall-clock bound wraps the subprocess
in ``asyncio.wait_for``; a timeout ``docker kill``s the container and reports
``timed_out``. Teardown relies on ``--rm`` but is *verified* — a container that
``docker inspect`` still finds marks the executor permanently unusable, because
an un-torn-down sandbox is an isolation breach, not a warning.

Honest boundary: on macOS + colima this yields DEVELOPMENT-grade evidence only.
A production isolation gate needs a dedicated Linux runner (cgroup v2 + rootless
+ seccomp/AppArmor). This module builds the same argv either way; only the
runtime underneath differs. Dependency rule: stdlib ``subprocess``/``asyncio``
calling the ``docker`` CLI — no docker-py, no new Python dependency.
"""
from __future__ import annotations

import asyncio
import base64
import os
import shlex
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC

# Host-side environment keys the ``docker`` CLI itself needs to reach the daemon
# (colima socket, config dir). This is the *launcher* process env, NOT the
# container env — the container gets only the explicit ``-e`` whitelist.
_HOST_ENV_KEYS = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR", "DOCKER_CONTEXT")

# How long to wait reaping a killed container after a wall-clock timeout.
_REAP_TIMEOUT_S = 5

# Per-stream capture cap (bytes). The host reads at most this much of stdout and
# of stderr into memory; anything beyond is drained-and-discarded, not buffered —
# so a container spewing continuous output (a `yes`-style oversized-output DoS)
# can never grow the backend process's memory, independent of the container's own
# --memory bound (which limits the container, not the host reading its pipe).
_MAX_STREAM_CHARS = 64 * 1024

# Chunk size for the draining reader.
_READ_CHUNK = 64 * 1024


class ExecutorError(Exception):
    """A sandbox run could not be carried out (spawn failure, unusable executor)."""


class SandboxTeardownError(ExecutorError):
    """A container could not be confirmed removed after its run. The executor is
    marked unusable: leaving a sandbox alive is an isolation breach, not a retry."""


@dataclass
class SandboxLimits:
    memory_mb: int = 256
    cpus: float = 0.5
    pids: int = 128
    wall_clock_s: int = 20
    scratch_mb: int = 64
    network: str = "none"          # default: no network at all


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    teardown_proof: dict = field(default_factory=dict)
    truncated: bool = False        # a captured stream exceeded _MAX_STREAM_CHARS


async def _exec_run(argv: list[str], env: dict) -> asyncio.subprocess.Process:
    """Spawn the container run (module-level so tests can substitute a fake)."""
    return await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )


async def _read_capped(stream, limit: int) -> tuple[bytes, bool]:
    """Read ``stream`` to EOF but KEEP at most ``limit`` bytes. Bytes past the cap
    are read-and-discarded (bounded transient memory, never accumulated) so a
    runaway producer cannot balloon the host — while still draining the pipe so
    the container isn't blocked on a full stdout buffer. Returns
    ``(captured_bytes, truncated)``."""
    if stream is None:
        return b"", False
    buf = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        if len(buf) < limit:
            keep = chunk[: limit - len(buf)]
            buf += keep
            if len(chunk) > len(keep):
                truncated = True
        else:
            truncated = True
        # loop continues: beyond the cap we keep reading `chunk` only to drain it,
        # discarding immediately — `buf` never grows past `limit`.
    return bytes(buf), truncated


async def _exec_cmd(argv: list[str], env: dict) -> tuple[int, str, str]:
    """Run a short docker control command (inspect/kill/rm), returning
    ``(returncode, stdout, stderr)``. Module-level for the same reason."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


class OciExecutor:
    def __init__(self, *, image: str, limits: SandboxLimits, runner: str = "docker",
                 user: str = "65534:65534") -> None:
        self.image = image
        self.limits = limits
        self.runner = runner
        self.user = user
        # Flipped true if a teardown cannot be confirmed — the executor then
        # refuses every further run (a leaked sandbox must not be reused).
        self._broken = False

    # ── pure argv assembly (the isolation contract; no I/O) ───────────

    def build_run_argv(self, *, name: str, argv: list[str],
                       scratch_files: dict[str, str] | None = None,
                       env: dict | None = None) -> list[str]:
        L = self.limits
        cmd = [
            self.runner, "run", "--rm",
            "--name", name,
            "--network", L.network,
            "--read-only",
            "--tmpfs", f"/scratch:size={L.scratch_mb}m,mode=1777",
            "--memory", f"{L.memory_mb}m",
            "--cpus", str(L.cpus),
            "--pids-limit", str(L.pids),
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--user", self.user,
            "--workdir", "/scratch",
        ]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        cmd.append(self.image)
        if scratch_files:
            # Materialise files into the tmpfs from inside the container — never a
            # host bind mount. base64 keeps arbitrary content shell-safe.
            cmd += ["sh", "-c", self._scratch_prologue(scratch_files, argv)]
        else:
            cmd += list(argv)
        return cmd

    @staticmethod
    def _scratch_prologue(scratch_files: dict[str, str], argv: list[str]) -> str:
        steps: list[str] = []
        for path, content in scratch_files.items():
            target = path if path.startswith("/") else f"/scratch/{path}"
            b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
            steps.append(f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(target)}")
        steps.append(" ".join(shlex.quote(a) for a in argv))
        return " && ".join(steps)

    # ── execution ─────────────────────────────────────────────────────

    async def run(self, *, argv: list[str], scratch_files: dict[str, str] | None = None,
                  env: dict | None = None) -> SandboxResult:
        if self._broken:
            raise ExecutorError("executor is unusable after an unverified teardown")

        name = f"lab-oci-{uuid.uuid4().hex[:12]}"
        docker_argv = self.build_run_argv(name=name, argv=argv, scratch_files=scratch_files, env=env)
        host_env = self._host_env()

        timed_out = False
        out_b = err_b = b""
        truncated = False
        proc = await _exec_run(docker_argv, host_env)
        try:
            # Read stdout + stderr concurrently (both must be drained or a full
            # pipe would block the container), each capped at _MAX_STREAM_CHARS so
            # the host memory stays bounded no matter how much the container emits.
            (out_b, out_trunc), (err_b, err_trunc) = await asyncio.wait_for(
                asyncio.gather(
                    _read_capped(proc.stdout, _MAX_STREAM_CHARS),
                    _read_capped(proc.stderr, _MAX_STREAM_CHARS),
                ),
                timeout=self.limits.wall_clock_s,
            )
            truncated = out_trunc or err_trunc
            await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_S)
            exit_code = proc.returncode if proc.returncode is not None else -1
        except asyncio.TimeoutError:
            timed_out = True
            await self._docker(["kill", name])   # TERM the runaway container
            try:
                await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_S)
            except Exception:
                pass
            exit_code = proc.returncode if proc.returncode is not None else -1

        # --rm reaps on exit; force-remove is belt-and-braces (no-op if already gone).
        await self._docker(["rm", "-f", name])
        teardown_proof = await self._verify_teardown(name)

        return SandboxResult(
            exit_code=exit_code,
            stdout=self._decode(out_b),
            stderr=self._decode(err_b),
            timed_out=timed_out,
            teardown_proof=teardown_proof,
            truncated=truncated,
        )

    def as_broker_executor(self):
        """Return an ``async (tool_name, args) -> dict`` matching the Broker's
        ``execute_action`` executor slot. Only code/shell tools reach here; the
        Broker redacts and stores the returned dict. A container handle is never
        exposed — the caller sees only exit code + captured streams + teardown
        proof."""
        async def _execute(tool_name: str, args: dict) -> dict:
            command = _command_from_args(args)
            if command is None:
                return {"tool": tool_name, "ok": False,
                        "summary": f"{tool_name}: no executable command in args"}
            res = await self.run(argv=["sh", "-c", command])
            ok = res.exit_code == 0 and not res.timed_out
            return {
                "tool": tool_name,
                "ok": ok,
                "exit_code": res.exit_code,
                "timed_out": res.timed_out,
                "stdout": res.stdout,   # already capped at capture time
                "stderr": res.stderr,
                "truncated": res.truncated,
                "summary": f"executed {tool_name} in oci sandbox (exit {res.exit_code})",
                "teardown": res.teardown_proof,
            }
        return _execute

    # ── helpers ───────────────────────────────────────────────────────

    async def _docker(self, subargv: list[str]) -> tuple[int, str, str]:
        return await _exec_cmd([self.runner] + subargv, self._host_env())

    async def _verify_teardown(self, name: str) -> dict:
        rc, _out, _err = await self._docker(["inspect", name])
        removed = rc != 0  # inspect fails ⇒ no such container ⇒ removed
        proof = {"removed": removed, "name": name, "checked_at": datetime.now(UTC).isoformat()}
        if not removed:
            self._broken = True
            raise SandboxTeardownError(f"container {name} still present after run")
        return proof

    @staticmethod
    def _host_env() -> dict:
        return {k: os.environ[k] for k in _HOST_ENV_KEYS if k in os.environ}

    @staticmethod
    def _decode(data: bytes) -> str:
        return (data or b"").decode("utf-8", "replace")


def _command_from_args(args: dict) -> str | None:
    """Extract the shell command a code/shell tool wants to run."""
    if not isinstance(args, dict):
        return None
    for key in ("command", "code", "script", "cmd"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None
