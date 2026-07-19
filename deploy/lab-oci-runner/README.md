# Lab OCI Runner — V11 isolation evidence

This directory prepares a **dedicated Linux runner** to collect the real
container-isolation evidence the lab OCI gate (V11) requires, and documents the
hard boundary around it.

## Why this exists (hard constraint)

`backend/tests/integration/test_lab_executor_oci.py` runs REAL containers via the
`docker` CLI (marked `@pytest.mark.lab_oci`, excluded from the default gate by
`addopts = -m 'not lab_oci'`). On macOS + colima / Docker Desktop these are only
**development-grade** checks. Per the kickoff hard constraint #2:

> colima/Docker Desktop 不是生产隔离证据；`lab_oci` 门必须在专用 Linux runner
> (cgroup v2、rootless OCI、seccomp/AppArmor、受控 egress) 通过后才可在 staging
> 启用真实执行。

So `settings.lab_oci_enabled` **stays `False`** until this gate passes on a
qualifying Linux runner, and even then real execution is enabled in staging
first, behind a manual canary.

## What the executor enforces (so you know what the evidence proves)

`app/lab/sandbox/oci_executor.py::build_run_argv` assembles `docker run` flags
(pure + unit-tested without a daemon):

- `--network none` (default `SandboxLimits.network`) — no egress at all;
- `--read-only` rootfs + a size-quota'd `--tmpfs /scratch` — no persistent or host FS;
- `--cap-drop ALL`, `--security-opt no-new-privileges`, non-root `--user`;
- `--memory`, `--cpus`, `--pids-limit` quotas; wall-clock via `asyncio.wait_for`;
- no host bind mount, no docker socket;
- `--rm` teardown that is *verified* (a still-present container marks the
  executor permanently unusable — an un-torn-down sandbox is an isolation breach).

The runner underneath does not change the argv; it changes whether the evidence
is production-grade. seccomp + AppArmor come from the runtime defaults on a
proper Linux host — that is precisely what colima on macOS cannot vouch for.

## Runner requirements

- Linux with **cgroup v2** (`/sys/fs/cgroup/cgroup.controllers` present).
- **Rootless** docker (rootless mode) or podman.
- **seccomp** available and the default profile applied (do NOT run tests with
  `--security-opt seccomp=unconfined`).
- **AppArmor** (or SELinux) enabled (`aa-status --enabled`).
- Unprivileged user namespaces enabled (`kernel.unprivileged_userns_clone=1`).
- **Controlled egress**: front the host with an explicit egress proxy/allowlist;
  the network-isolation tests assume the sandbox has no open internet.

## Usage

```bash
# on the dedicated Linux runner, from a checkout of this repo
LAB_OCI_IMAGE=alpine:latest deploy/lab-oci-runner/provision-runner.sh
```

The script fingerprints the host, verifies the security posture, pulls the image,
creates a venv, installs `backend[dev]`, and runs:

```bash
python3 -m pytest -m lab_oci tests/integration/test_lab_executor_oci.py -v
```

Evidence (env fingerprint + pytest output) is written to
`docs/renders/lab-oci-evidence/{env,pytest}-<UTC timestamp>.txt`. Commit that
bundle as the V11 record.

## After a green run

1. Attach the evidence bundle to the V11 record.
2. In **staging only**, set `LAB_OCI_ENABLED=true` and `LAB_OCI_IMAGE=<image>`;
   canary a single run; watch teardown-verification + Broker-only telemetry.
3. Promote to production only after the staging canary + kill/rollback runbook
   drill (T8) pass.

This script never flips `lab_oci_enabled` — enabling is a deliberate, separate,
reviewed step.
