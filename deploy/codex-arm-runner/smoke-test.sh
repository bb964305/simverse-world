#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_DIR/backend/.env}"
RUNNER="${CODEX_CONTAINER_RUNNER:-docker}"
CODEX_VERSION="${CODEX_VERSION:-0.144.4}"
CODEX_SHA256="${CODEX_SHA256:-4d07243ef4ae6786b8b321d7aea3f9be4e1d2c597ae5407e7c1b9873334082b2}"
IMAGE="${CODEX_ARM_IMAGE:-simverse-codex-arm-probe:$CODEX_VERSION}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "model environment file not found: $ENV_FILE" >&2
  exit 2
fi

if ! command -v "$RUNNER" >/dev/null 2>&1; then
  echo "container runner not found: $RUNNER" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${LAB_RUN_TOKEN:?LAB_RUN_TOKEN must contain a short-lived gateway token}"
: "${CODEX_PROVIDER_BASE_URL:?CODEX_PROVIDER_BASE_URL must end in /v1}"
export CODEX_EPHEMERAL="${CODEX_EPHEMERAL:-false}"
export CODEX_PROMPT="Run uname -m with the shell tool, then reply with the architecture and CODEX_ARM_PROBE_OK."

echo "building $IMAGE for linux/arm64"
"$RUNNER" build \
  --platform linux/arm64 \
  --build-arg "CODEX_VERSION=$CODEX_VERSION" \
  --build-arg "CODEX_SHA256=$CODEX_SHA256" \
  --tag "$IMAGE" \
  "$SCRIPT_DIR"

image_arch="$("$RUNNER" image inspect "$IMAGE" --format '{{.Architecture}}')"
if [[ "$image_arch" != "arm64" ]]; then
  echo "unexpected image architecture: $image_arch" >&2
  exit 1
fi

echo "running isolated Codex probe through the Lab model gateway"
probe_output="$("$RUNNER" run --rm \
  --platform linux/arm64 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 128 \
  --memory 1g \
  --cpus 1 \
  --tmpfs /tmp:rw,noexec,nosuid,size="${CODEX_TMPFS_SIZE:-64m}" \
  --env LAB_RUN_TOKEN \
  --env CODEX_PROVIDER_BASE_URL \
  --env CODEX_EPHEMERAL \
  --env CODEX_PROMPT \
  "$IMAGE")"

printf '%s\n' "$probe_output"

printf '%s\n' "$probe_output" | jq -e -s '
  all(.[]; .type != "error" and .type != "turn.failed")
  and any(.[]; .type == "turn.completed")
  and any(.[];
    .type == "item.completed"
    and .item.type == "command_execution"
    and ((.item.command // "") | contains("uname -m"))
    and ((.item.aggregated_output // "") | contains("aarch64"))
  )
  and any(.[];
    .type == "item.completed"
    and .item.type == "agent_message"
    and ((.item.text // "") | contains("CODEX_ARM_PROBE_OK"))
  )
' >/dev/null

echo "Codex ARM64 Responses-gateway tool probe passed"
