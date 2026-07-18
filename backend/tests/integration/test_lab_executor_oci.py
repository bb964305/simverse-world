"""P2-E — V11 adversarial isolation evidence for the rootless OCI executor.

REAL containers via the local docker CLI. Marked ``@pytest.mark.lab_oci`` and
EXCLUDED from the default gate (pyproject ``addopts = -m 'not lab_oci'``); opt in
with ``-m lab_oci``. The ``oci_ready`` fixture skips (never fails) when docker or
the image is unavailable, so this file is import-safe and inert in the normal
suite.

HONEST BOUNDARY: on macOS + colima these are DEVELOPMENT-grade isolation checks.
A production isolation gate requires a dedicated Linux runner (cgroup v2 +
rootless + seccomp/AppArmor); Docker Desktop / colima on macOS is explicitly NOT
production isolation evidence (PRD §Data Plane). What these prove is that the
executor assembles and enforces the right guarantees end-to-end on a real OCI
runtime: no host FS, no network, no docker socket, non-root, read-only rootfs,
pids/wall-clock quotas, and verified teardown — plus the Broker-only boundary.
"""
import json
import subprocess

import pytest

from app.lab.sandbox.oci_executor import OciExecutor, SandboxLimits

pytestmark = pytest.mark.lab_oci

_IMAGE = "alpine:latest"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


def _image_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "image", "inspect", _IMAGE], capture_output=True, timeout=15
        ).returncode == 0
    except Exception:
        return False


@pytest.fixture(scope="module")
def oci_ready():
    """Skip the whole module (don't fail) when the real runtime isn't here. The
    probe runs only when a lab_oci test executes, so the default gate never pays
    for it."""
    if not _docker_available():
        pytest.skip("docker unavailable (colima not running) — lab_oci evidence not collectable here")
    if not _image_available():
        pytest.skip(f"image {_IMAGE} not pulled — `docker pull {_IMAGE}` to collect evidence")


def _ex(**limit_over) -> OciExecutor:
    return OciExecutor(image=_IMAGE, limits=SandboxLimits(**limit_over))


# ── 1. scratch writable, host filesystem unreachable (no bind mount) ──

@pytest.mark.anyio
async def test_scratch_writable_but_no_host_bind(oci_ready, tmp_path):
    ex = _ex()
    write = await ex.run(argv=["sh", "-c", "echo scratch_ok > /scratch/x && cat /scratch/x"])
    assert write.exit_code == 0 and "scratch_ok" in write.stdout

    # Discriminating no-bind proof (Minor #3): a REAL host file with known content
    # is unreadable from inside by its host path (it WOULD be readable if any -v
    # bound the host tree), and the container's mount table lists no bind of the
    # host dir. A test against a never-existing path would pass even with a bind.
    marker = tmp_path / "host_secret.txt"
    marker.write_text("HOST_ONLY_SECRET_XYZ")
    read = await ex.run(argv=["sh", "-c", f'cat "{marker}" 2>&1 || echo NO_HOST_FS'])
    assert "HOST_ONLY_SECRET_XYZ" not in read.stdout  # host content never leaks in
    assert "NO_HOST_FS" in read.stdout
    mounts = await ex.run(argv=["cat", "/proc/mounts"])
    assert str(tmp_path) not in mounts.stdout          # no host bind mount present


# ── 2 & 3. no network: egress + metadata endpoint both unreachable ────

@pytest.mark.anyio
async def test_no_network_egress_blocked(oci_ready):
    ex = _ex()
    res = await ex.run(argv=["sh", "-c", "wget -T 3 -q -O- http://example.org || echo NETFAIL"])
    assert "NETFAIL" in res.stdout  # --network none ⇒ no egress


@pytest.mark.anyio
async def test_no_network_metadata_endpoint_unreachable(oci_ready):
    ex = _ex()
    res = await ex.run(
        argv=["sh", "-c", "wget -T 3 -q -O- http://169.254.169.254/latest/meta-data/ || echo BLOCKED"]
    )
    assert "BLOCKED" in res.stdout  # cloud metadata SSRF impossible with no network


# ── 4. no docker socket inside the sandbox ────────────────────────────

@pytest.mark.anyio
async def test_no_docker_socket_in_container(oci_ready):
    ex = _ex()
    res = await ex.run(argv=["ls", "/var/run/docker.sock"])
    assert res.exit_code != 0  # daemon socket not mounted ⇒ no container escape


# ── 5. non-root uid ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_runs_as_non_root(oci_ready):
    ex = _ex()
    res = await ex.run(argv=["id", "-u"])
    assert res.exit_code == 0
    assert res.stdout.strip() != "0" and res.stdout.strip() != ""


# ── 6. read-only rootfs, writable scratch ─────────────────────────────

@pytest.mark.anyio
async def test_readonly_rootfs_scratch_writable(oci_ready):
    ex = _ex()
    # Discriminating read-only proof (Minor #3): the ROOT mount is ``ro`` in
    # /proc/mounts. A non-root ``touch /root_marker`` failing is NOT proof — it
    # EACCESes on a writable rootfs too, so it has no discriminating power.
    mounts = (await ex.run(argv=["cat", "/proc/mounts"])).stdout
    root_opts = [
        line.split()[3] for line in mounts.splitlines()
        if len(line.split()) >= 4 and line.split()[1] == "/"
    ]
    assert root_opts and any("ro" in opts.split(",") for opts in root_opts)

    # scratch tmpfs stays writable.
    rw = await ex.run(argv=["sh", "-c", "touch /scratch/ok && echo WROTE"])
    assert rw.exit_code == 0 and "WROTE" in rw.stdout


# ── 7. pids quota bounds a fork storm ─────────────────────────────────

@pytest.mark.anyio
async def test_pids_limit_bounds_fork_storm(oci_ready):
    ex = _ex(pids=16)
    res = await ex.run(
        argv=["sh", "-c", "n=0; while [ $n -lt 300 ]; do sleep 30 & n=$((n+1)); done; echo tried=$n"]
    )
    # busybox prints "can't fork" once the cgroup pids cap is hit.
    assert "fork" in res.stderr.lower() or res.exit_code != 0


# ── 8. wall-clock timeout kills the container ─────────────────────────

@pytest.mark.anyio
async def test_wall_clock_timeout_kills_container(oci_ready):
    ex = _ex(wall_clock_s=2)
    res = await ex.run(argv=["sleep", "60"])
    assert res.timed_out is True
    assert res.teardown_proof["removed"] is True  # killed container is still torn down


# ── 9. teardown proof — container gone after every run ────────────────

@pytest.mark.anyio
async def test_teardown_removes_container(oci_ready):
    ex = _ex()
    res = await ex.run(argv=["true"])
    name = res.teardown_proof["name"]
    assert res.teardown_proof["removed"] is True
    # Independent confirmation: docker inspect can no longer find it.
    inspect = subprocess.run(["docker", "inspect", name], capture_output=True)
    assert inspect.returncode != 0


# ── 11. oversized output is bounded (host DoS defence, Important #1) ──

@pytest.mark.anyio
async def test_oversized_output_is_capped(oci_ready):
    ex = _ex()
    # ~1 MiB to stdout; the host must keep at most the cap and flag truncation —
    # never buffer the whole stream (transient host-memory DoS).
    res = await ex.run(
        argv=["awk", "BEGIN{for(i=0;i<50000;i++)print \"ABCDEFGHIJABCDEFGHIJ\"}"]
    )
    assert res.truncated is True
    assert len(res.stdout.encode("utf-8")) <= 64 * 1024


# ── 10. Broker-only boundary: approved code action → clean result ─────

@pytest.mark.anyio
async def test_broker_executor_runs_code_action_and_returns_clean_result(oci_ready, db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_grant_secret", "test-secret", raising=False)
    from app.lab import broker, grants

    db = db_session
    token, claims = await grants.issue_run_grant(
        db, tenant_id="t1", task_id="tk1", run_id="r1", agent_id="a1",
        capabilities=["code"], fencing_epoch=0,
    )
    executor = _ex().as_broker_executor()

    # An approved (R1 allow) code action executes inside the sandbox.
    args_ok = {"command": "echo integ-ok"}
    action = await broker.request_action(db, claims=claims, token=token, tool_name="code.run", args=args_ok)
    assert action.status == "approved"
    res = await broker.execute_action(
        db, action_id=action.id, claims=claims, executor=executor, args=args_ok,
    )
    assert res.status == "succeeded"
    assert "integ-ok" in json.dumps(res.result_json)  # sandbox stdout reached the Broker

    # A host-read attempt fails INSIDE the sandbox, yet the Broker still gets a
    # clean, redacted result dict — no host data leaks out, run stays healthy.
    args_bad = {"command": "cat /etc/shadow_host_only 2>&1 || echo NO_HOST_ACCESS"}
    action2 = await broker.request_action(db, claims=claims, token=token, tool_name="shell.exec", args=args_bad)
    res2 = await broker.execute_action(
        db, action_id=action2.id, claims=claims, executor=executor, args=args_bad,
    )
    assert res2.status == "succeeded"
    assert "NO_HOST_ACCESS" in json.dumps(res2.result_json)
