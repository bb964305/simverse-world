# Lab OCI isolation evidence (V11) — dedicated Linux runner

Recovery plan Phase 8, step 1. This bundle is the **production-grade** OCI
isolation evidence the PRD requires before `lab_oci_enabled` may be considered
for staging — captured on a real dedicated Linux runner, NOT colima/Docker
Desktop (which the PRD explicitly rejects as dev-grade).

## Runner (2026-07-19)

- Host: Oracle Cloud aarch64, Ubuntu 22.04.5 LTS, kernel `6.8.0-1047-oracle`.
- Container runtime: Docker **29.2.1 rootless** (daemon `unix:///run/user/1001/docker.sock`).
- cgroup: unified **cgroup v2** (`cgroup2fs`), systemd driver; controllers
  delegated to the rootless user: `cpuset cpu io memory pids` (the `cpu`/`cpuset`
  delegation is required — see the NanoCPUs note below and `provision-runner.sh`).
- LSM: `lockdown,capability,landlock,yama,apparmor`; in-container **Seccomp: 2
  (filtered)**, one filter loaded — the default seccomp profile is applied to
  sandbox containers.

## Result

`LAB_OCI_IMAGE=alpine:latest LAB_OCI_REQUIRED=1 pytest -m lab_oci
tests/integration/test_lab_executor_oci.py` → **11 passed, 0 skipped, 0 failed**
(`pytest-v11-*.log`). `LAB_OCI_REQUIRED=1` is the dedicated-gate mode: a missing
Linux/daemon/image/rootless/cgroup-v2 prerequisite FAILS the suite, so this green
run cannot be an all-skipped no-op — every one of the 11 adversarial checks ran
inside a real container.

Checks proven: scratch writable + no host bind mount (a real host secret is
unreadable and absent from `/proc/mounts`), no-network egress blocked, cloud
metadata endpoint (169.254.169.254 SSRF) unreachable, no docker socket in the
container, non-root uid, read-only rootfs with writable scratch, pids quota
bounds a fork storm, wall-clock timeout kills + tears down the container,
teardown removes the container (independently confirmed via `docker inspect`),
oversized output is capped host-side, and the Broker-only boundary runs an
approved code action and returns a clean redacted result while a host-read
attempt fails inside the sandbox.

## First run caught a real defect in the runner setup

The initial run failed 8/11 with
`docker: Error response from daemon: NanoCPUs can not be set ... cgroup is not
mounted` (exit 125): the rootless user had only `memory pids` delegated. The 3
"passes" were spurious (a container that fails to start still yields exit≠0 /
teardown-true, satisfying those assertions). After delegating `cpu cpuset` via
`/etc/systemd/system/user@.service.d/delegate.conf` and restarting the user
session, all 11 pass legitimately. `provision-runner.sh` now performs this
delegation.

## Honest boundary

- This proves the **OCI executor's isolation contract** on a qualifying host. It
  does NOT flip `lab_oci_enabled` on — that stays `False`, pending a staging
  canary (the executor still runs Mock work until a real Adapter is selected).
- **P7 (real runtime Adapter) remains BLOCKED**: this host is an OCI *runner*, it
  provides no real Hermes/OpenClaw/computer-use agent-runtime endpoint. No real
  Adapter was exercised or scored; the ADR stays 未选型.
