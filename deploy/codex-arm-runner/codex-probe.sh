#!/bin/sh
set -eu

: "${LAB_RUN_TOKEN:?LAB_RUN_TOKEN is required}"
: "${CODEX_PROVIDER_BASE_URL:?CODEX_PROVIDER_BASE_URL is required}"

prompt="${CODEX_PROMPT:-Call exec_command to run uname -m without requesting escalated permissions. Then reply with the architecture and CODEX_ARM_PROBE_OK.}"
export CODEX_HOME="${CODEX_HOME:-/tmp/codex-home}"
mkdir -p "$CODEX_HOME"
cp /etc/codex/config.toml "$CODEX_HOME/config.toml"

codex --version >&2

set -- codex --ask-for-approval never exec
if [ "${CODEX_EPHEMERAL:-false}" = "true" ]; then
  set -- "$@" --ephemeral
fi

exec "$@" \
  --json \
  --sandbox danger-full-access \
  --skip-git-repo-check \
  --model lab-auto \
  --config "model_providers.lab_gateway.base_url=\"$CODEX_PROVIDER_BASE_URL\"" \
  "$prompt"
