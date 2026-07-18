"""P2-E — OCI executor spec unit tests (main gate, NO real container).

These pin the *argv assembly* and *control flow* of the rootless OCI sandbox
executor without ever starting a container, so they run in every gate on any
machine (docker not required). The real-container adversarial evidence lives in
``tests/integration/test_lab_executor_oci.py`` behind ``@pytest.mark.lab_oci``.

The load-bearing invariants proven here (V11, PRD §Data Plane rootless OCI):
docker run always carries ``--network none``/``--read-only``/``--cap-drop ALL``/
``--user <nonroot>``/``--pids-limit``/``--security-opt no-new-privileges``, a
size-quota'd ``--tmpfs /scratch`` and NO host bind mount / NO docker socket; the
wall-clock timeout kills the container and reports ``timed_out``; and a teardown
that cannot confirm removal marks the executor permanently unusable.
"""
import asyncio

import pytest

from app.lab.sandbox import oci_executor as oci
from app.lab.sandbox.oci_executor import (
    ExecutorError,
    OciExecutor,
    SandboxLimits,
    SandboxResult,
    SandboxTeardownError,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _val_after(argv: list[str], flag: str) -> str:
    """The token immediately following ``flag`` in ``argv`` (the flag's value)."""
    i = argv.index(flag)
    return argv[i + 1]


def _executor(**limit_over) -> OciExecutor:
    return OciExecutor(image="alpine:latest", limits=SandboxLimits(**limit_over))


# ── fake subprocess plumbing (monkeypatched onto the module) ──────────

class _FakeProc:
    def __init__(self, out: bytes = b"", err: bytes = b"", rc: int = 0, hang: bool = False):
        self._out, self._err, self.returncode, self._hang = out, err, rc, hang

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(30)
        return self._out, self._err

    async def wait(self):
        if self._hang:
            await asyncio.sleep(30)
        return self.returncode


def _patch_run(monkeypatch, proc: _FakeProc):
    async def _fake_exec_run(argv, env):
        return proc
    monkeypatch.setattr(oci, "_exec_run", _fake_exec_run)


def _patch_cmd(monkeypatch, *, inspect_rc: int, sink: list | None = None):
    async def _fake_exec_cmd(argv, env):
        if sink is not None:
            sink.append(list(argv))
        # ``docker inspect`` decides teardown; everything else is best-effort.
        if len(argv) >= 2 and argv[1] == "inspect":
            return inspect_rc, "", "" if inspect_rc else "[]"
        return 0, "", ""
    monkeypatch.setattr(oci, "_exec_cmd", _fake_exec_cmd)


# ── argv assembly (the isolation contract) ────────────────────────────

def test_build_argv_carries_every_isolation_flag():
    ex = _executor(memory_mb=128, cpus=0.25, pids=64, scratch_mb=32, network="none")
    argv = ex.build_run_argv(name="lab-oci-abc", argv=["id", "-u"])

    assert "--rm" in argv
    assert _val_after(argv, "--network") == "none"
    assert "--read-only" in argv
    assert _val_after(argv, "--cap-drop") == "ALL"
    assert _val_after(argv, "--pids-limit") == "64"
    assert _val_after(argv, "--security-opt") == "no-new-privileges"
    assert _val_after(argv, "--memory") == "128m"
    assert _val_after(argv, "--cpus") == "0.25"
    assert _val_after(argv, "--workdir") == "/scratch"
    assert _val_after(argv, "--name") == "lab-oci-abc"

    user = _val_after(argv, "--user")
    assert user and user.split(":")[0] != "0"  # never root

    tmpfs = _val_after(argv, "--tmpfs")
    assert tmpfs.startswith("/scratch:") and "size=32m" in tmpfs

    # image then the caller's argv are the tail.
    assert argv[-3:] == ["alpine:latest", "id", "-u"]


def test_build_argv_has_no_bind_mount_or_docker_socket():
    ex = _executor()
    argv = ex.build_run_argv(name="n", argv=["true"])
    assert "-v" not in argv and "--volume" not in argv
    assert not any("docker.sock" in tok for tok in argv)
    assert not any(tok.startswith("/var/run") for tok in argv)


def test_build_argv_env_is_explicit_whitelist_only():
    ex = _executor()
    argv = ex.build_run_argv(name="n", argv=["env"], env={"FOO": "bar"})
    # only the explicit key is injected; no host env passthrough.
    i = argv.index("-e")
    assert argv[i + 1] == "FOO=bar"
    assert not any(tok.startswith("PATH=") or tok.startswith("HOME=") for tok in argv)


def test_build_argv_scratch_files_write_via_prologue_not_bind():
    import base64
    ex = _executor()
    argv = ex.build_run_argv(
        name="n", argv=["sh", "/scratch/main.sh"], scratch_files={"main.sh": "echo hi"},
    )
    assert argv[-3] == "sh" and argv[-2] == "-c"
    prologue = argv[-1]
    assert base64.b64encode(b"echo hi").decode() in prologue
    assert "/scratch/main.sh" in prologue
    assert "-v" not in argv and "--volume" not in argv  # files land in tmpfs, never a bind


# ── run() control flow (fake subprocess) ──────────────────────────────

@pytest.mark.anyio
async def test_run_success_reports_output_and_verified_teardown(monkeypatch):
    ex = _executor()
    _patch_run(monkeypatch, _FakeProc(out=b"65534\n", err=b"", rc=0))
    _patch_cmd(monkeypatch, inspect_rc=1)  # inspect non-zero => container gone
    res = await ex.run(argv=["id", "-u"])
    assert isinstance(res, SandboxResult)
    assert res.exit_code == 0 and res.stdout == "65534\n"
    assert res.timed_out is False
    assert res.teardown_proof["removed"] is True
    assert res.teardown_proof["name"]


@pytest.mark.anyio
async def test_run_timeout_kills_container_and_flags_timed_out(monkeypatch):
    ex = _executor(wall_clock_s=1)
    monkeypatch.setattr(oci, "_REAP_TIMEOUT_S", 0.05)
    _patch_run(monkeypatch, _FakeProc(hang=True))  # never completes
    calls: list = []
    _patch_cmd(monkeypatch, inspect_rc=1, sink=calls)
    res = await ex.run(argv=["sleep", "60"])
    assert res.timed_out is True
    assert any(c[:2] == ["docker", "kill"] for c in calls)  # container was killed


@pytest.mark.anyio
async def test_teardown_that_cannot_confirm_removal_marks_executor_broken(monkeypatch):
    ex = _executor()
    _patch_run(monkeypatch, _FakeProc(out=b"", err=b"", rc=0))
    _patch_cmd(monkeypatch, inspect_rc=0)  # inspect succeeds => container STILL present
    with pytest.raises(SandboxTeardownError):
        await ex.run(argv=["true"])
    # once teardown can't be confirmed, the executor refuses further use.
    with pytest.raises(ExecutorError):
        await ex.run(argv=["true"])


@pytest.mark.anyio
async def test_as_broker_executor_returns_clean_dict(monkeypatch):
    ex = _executor()

    async def _fake_run(*, argv, scratch_files=None, env=None):
        assert argv[:2] == ["sh", "-c"]
        return SandboxResult(exit_code=0, stdout="integ-ok\n", stderr="",
                             timed_out=False, teardown_proof={"removed": True, "name": "n"})
    monkeypatch.setattr(ex, "run", _fake_run)

    fn = ex.as_broker_executor()
    out = await fn("code.run", {"command": "echo integ-ok"})
    assert out["tool"] == "code.run"
    assert out["ok"] is True and out["exit_code"] == 0
    assert "integ-ok" in out["stdout"]
    assert out["teardown"]["removed"] is True
