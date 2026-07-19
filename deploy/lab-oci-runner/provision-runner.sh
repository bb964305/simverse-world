#!/usr/bin/env bash
# Provision a DEDICATED Linux runner for lab OCI isolation evidence (V11).
#
# Honest boundary (kickoff 硬约束 #2): colima / Docker Desktop on macOS is NOT
# production isolation evidence. The lab_oci gate must pass on a real Linux host
# with cgroup v2 + rootless OCI + seccomp/AppArmor + controlled egress before
# lab_oci_enabled may be flipped on in staging. This script prepares such a host
# and then runs the opt-in gate, capturing an evidence bundle.
#
# Idempotent-ish: safe to re-run; package installs are apt-guarded. Requires a
# Debian/Ubuntu host with sudo. Does NOT enable lab_oci in the app — it only
# collects evidence.
set -euo pipefail

IMAGE="${LAB_OCI_IMAGE:-alpine:latest}"
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
EVIDENCE_DIR="${EVIDENCE_DIR:-$REPO_DIR/docs/renders/lab-oci-evidence}"
RUNNER="${LAB_OCI_RUNNER:-docker}"   # docker (rootless) or podman

log() { printf '\n=== %s ===\n' "$*"; }

log "0. host fingerprint"
uname -a
[ -f /sys/fs/cgroup/cgroup.controllers ] && echo "cgroup v2: yes" || { echo "cgroup v2: NO — abort (v1 host is not acceptable)"; exit 2; }
cat /etc/os-release 2>/dev/null | head -2 || true

log "1. rootless container runtime"
if ! command -v "$RUNNER" >/dev/null 2>&1; then
  if [ "$RUNNER" = "docker" ]; then
    curl -fsSL https://get.docker.com/rootless | sh
    export PATH="$HOME/bin:$PATH"
    export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/docker.sock"
  else
    sudo apt-get update && sudo apt-get install -y podman
  fi
fi
"$RUNNER" info >/dev/null || { echo "$RUNNER not usable rootless — fix before collecting evidence"; exit 3; }

log "2. security posture checks (must be enabled for production-grade evidence)"
# seccomp: docker applies the default profile unless --security-opt seccomp=unconfined.
# AppArmor: kernel module must be loaded; docker applies docker-default.
grep -q seccomp /proc/self/status && echo "seccomp: available" || echo "seccomp: NOT reported by /proc — verify kernel config"
if command -v aa-status >/dev/null 2>&1; then aa-status --enabled && echo "apparmor: enabled" || echo "apparmor: NOT enabled — install/enable before evidence"; else echo "apparmor: aa-status missing — install apparmor-utils"; fi
[ "$(cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo 1)" = "1" ] && echo "userns: enabled" || echo "userns: disabled — rootless needs it"

log "3. controlled egress note"
echo "The OCI executor runs containers with --network none by default (SandboxLimits.network)."
echo "For the egress-allowlist tests, front the runner with an explicit egress proxy/firewall;"
echo "do NOT rely on the host having open internet."

log "4. pull the test image"
"$RUNNER" pull "$IMAGE"

log "5. python env for the backend test suite"
cd "$REPO_DIR/backend"
python3 -m venv .venv-oci 2>/dev/null || true
. .venv-oci/bin/activate
pip install -q -e '.[dev]'

log "6. run the opt-in OCI isolation gate (V11)"
mkdir -p "$EVIDENCE_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
{
  echo "# lab_oci evidence — $STAMP"
  echo "runner: $RUNNER  image: $IMAGE"
  "$RUNNER" version 2>&1 | sed 's/^/  /'
  echo "host: $(uname -a)"
} > "$EVIDENCE_DIR/env-$STAMP.txt"

LAB_OCI_IMAGE="$IMAGE" python3 -m pytest -m lab_oci tests/integration/test_lab_executor_oci.py -v \
  2>&1 | tee "$EVIDENCE_DIR/pytest-$STAMP.txt"

log "done"
echo "Evidence written to: $EVIDENCE_DIR/{env,pytest}-$STAMP.txt"
echo "Only after this passes on a dedicated Linux runner may lab_oci_enabled be"
echo "flipped on (staging first, manual canary). This script does NOT flip it."
