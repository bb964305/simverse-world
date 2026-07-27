#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_DIR/backend/.env}"
RUNNER="${CODEX_CONTAINER_RUNNER:-docker}"
GATEWAY_IMAGE="simverse-lab-model-gateway:0.1.0"
RUNTIME_IMAGE="simverse-codex-runtime:0.1.0"
PROBE_IMAGE="simverse-codex-arm-probe:0.144.4"
GATEWAY_CONTAINER="simverse-model-gateway-functional"
RUNTIME_CONTAINER="simverse-codex-runtime-functional"
CLIENT_CONTAINER="simverse-codex-client-functional"
GATEWAY_NETWORK="simverse-codex-gateway-functional"
RUNTIME_NETWORK="simverse-codex-runtime-functional"

command -v "$RUNNER" >/dev/null 2>&1 || { echo "container runner not found" >&2; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 2; }

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
elif [[ -z "${LLM_API_KEY:-}" ]]; then
  echo "model environment file not found and LLM_API_KEY is not exported: $ENV_FILE" >&2
  exit 2
fi
: "${LLM_API_KEY:?LLM_API_KEY is required in $ENV_FILE}"

gateway_secret="$(openssl rand -hex 32)"
runtime_key="$(openssl rand -hex 32)"
cleanup() {
  "$RUNNER" rm -f "$CLIENT_CONTAINER" >/dev/null 2>&1 || true
  "$RUNNER" rm -f "$RUNTIME_CONTAINER" >/dev/null 2>&1 || true
  "$RUNNER" rm -f "$GATEWAY_CONTAINER" >/dev/null 2>&1 || true
  "$RUNNER" network rm "$RUNTIME_NETWORK" >/dev/null 2>&1 || true
  "$RUNNER" network rm "$GATEWAY_NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup
"$RUNNER" network create "$GATEWAY_NETWORK" >/dev/null
"$RUNNER" network create --internal "$RUNTIME_NETWORK" >/dev/null

if [[ "${SKIP_BUILD:-false}" != "true" ]]; then
  "$RUNNER" build -f "$SCRIPT_DIR/Dockerfile.gateway" -t "$GATEWAY_IMAGE" "$REPO_DIR"
  "$RUNNER" build -f "$SCRIPT_DIR/Dockerfile.runtime" -t "$RUNTIME_IMAGE" "$REPO_DIR"
  "$RUNNER" build -f "$SCRIPT_DIR/Dockerfile" -t "$PROBE_IMAGE" "$SCRIPT_DIR"
fi

"$RUNNER" run -d --name "$CLIENT_CONTAINER" --network "$RUNTIME_NETWORK" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 32 --memory 128m --cpus 0.25 \
  --entrypoint sleep "$PROBE_IMAGE" infinity >/dev/null
runtime_curl() {
  "$RUNNER" exec "$CLIENT_CONTAINER" curl "$@"
}

"$RUNNER" run -d --name "$GATEWAY_CONTAINER" --network "$GATEWAY_NETWORK" \
  -p 127.0.0.1:8096:8096 \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /var/lib/simverse:rw,noexec,nosuid,size=64m \
  -e LAB_MODEL_GATEWAY_BIND_HOST=0.0.0.0 \
  -e LAB_MODEL_GATEWAY_BIND_PORT=8096 \
  -e LAB_MODEL_GATEWAY_UPSTREAM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
  -e LAB_MODEL_GATEWAY_UPSTREAM_API_KEY="$LLM_API_KEY" \
  -e LAB_MODEL_GATEWAY_AUTH_SECRET="$gateway_secret" \
  -e LAB_MODEL_GATEWAY_LEDGER_PATH=/tmp/model-gateway.db \
  "$GATEWAY_IMAGE" >/dev/null
"$RUNNER" network connect "$RUNTIME_NETWORK" "$GATEWAY_CONTAINER"

for _ in {1..30}; do
  curl -fsS http://127.0.0.1:8096/readyz >/dev/null && break
  sleep 1
done
curl -fsS http://127.0.0.1:8096/readyz >/dev/null

issue_token() {
  local tier="$1" run_id="$2"
  "$RUNNER" run --rm --entrypoint python \
    -e LAB_MODEL_GATEWAY_AUTH_SECRET="$gateway_secret" \
    -e LAB_PROBE_MODEL_TIER="$tier" -e LAB_PROBE_RUN_ID="$run_id" \
    "$GATEWAY_IMAGE" -m app.lab.model_gateway.probe_token
}

for tier in low high; do
  run_id="arm-${tier}-$(date +%s)"
  token="$(issue_token "$tier" "$run_id")"
  if ! output="$("$RUNNER" run --rm --network "$RUNTIME_NETWORK" \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 128 --memory 1g --cpus 1 \
    --tmpfs /tmp:rw,noexec,nosuid,size=512m \
    -e LAB_RUN_TOKEN="$token" \
    -e CODEX_PROVIDER_BASE_URL=http://$GATEWAY_CONTAINER:8096/v1 \
    -e CODEX_EPHEMERAL=true \
    -e CODEX_PROMPT='Call exec_command to run uname -m without requesting escalated permissions. Then reply with the architecture and CODEX_ARM_PROBE_OK.' \
    "$PROBE_IMAGE" </dev/null)"; then
    printf '%s\n' "$output" >&2
    "$RUNNER" logs "$GATEWAY_CONTAINER" >&2 || true
    exit 1
  fi
  if ! printf '%s\n' "$output" | jq -e -s '
    all(.[]; .type != "error" and .type != "turn.failed")
    and any(.[]; .type == "turn.completed")
    and any(.[]; .type == "item.completed" and .item.type == "command_execution"
      and ((.item.aggregated_output // "") | contains("aarch64")))
    and any(.[]; .type == "item.completed" and .item.type == "agent_message"
      and ((.item.text // "") | contains("CODEX_ARM_PROBE_OK")))
  ' >/dev/null; then
    printf '%s\n' "$output" >&2
    "$RUNNER" logs "$GATEWAY_CONTAINER" >&2 || true
    exit 1
  fi
  usage="$(curl -fsS -H "Authorization: Bearer $token" http://127.0.0.1:8096/v1/lab/usage)"
  [[ "$(jq -r .model <<<"$usage")" == "deepseek-v4-$([[ "$tier" == low ]] && echo flash || echo pro)" ]]
  [[ "$(jq -r .total_tokens <<<"$usage")" -gt 0 ]]
  echo "$tier tier Codex tool loop passed"
done

runtime_token="$(issue_token low arm-runtime)"
"$RUNNER" run -d --name "$RUNTIME_CONTAINER" --network "$RUNTIME_NETWORK" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 512 --memory 8g --cpus 4 \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --tmpfs /var/lib/simverse/codex-runs:rw,nosuid,size=512m,uid=10001,gid=10001,mode=0700 \
  -e LAB_CODEX_RUNTIME_BIND_HOST=0.0.0.0 \
  -e LAB_CODEX_RUNTIME_BIND_PORT=8097 \
  -e LAB_CODEX_RUNTIME_API_KEY="$runtime_key" \
  -e LAB_CODEX_RUNTIME_SANDBOX=danger-full-access \
  -e LAB_CODEX_RUNTIME_TOTAL_CPU_CORES=4 \
  -e LAB_CODEX_RUNTIME_TOTAL_MEMORY_MB=8192 \
  "$RUNTIME_IMAGE" >/dev/null
for _ in {1..30}; do
  runtime_curl -fsS http://$RUNTIME_CONTAINER:8097/healthz >/dev/null && break
  sleep 1
done

session_id="$(runtime_curl -fsS -H "Authorization: Bearer $runtime_key" \
  -H 'Content-Type: application/json' http://$RUNTIME_CONTAINER:8097/runs \
  -d "{\"run_id\":\"arm-runtime\",\"tenant_id\":\"arm-probe\",\"scopes\":[\"code\"],\"budget_usd\":1.0,\"egress_allowlist\":[],\"model_tier\":\"low\",\"model_name\":\"deepseek-v4-flash\",\"model_policy_version\":\"arm-probe-v1\",\"resource_cpu_cores\":2,\"resource_memory_mb\":2048,\"model_gateway_base_url\":\"http://$GATEWAY_CONTAINER:8096/v1\",\"model_gateway_token\":\"$runtime_token\"}" | jq -r .session_id)"
runtime_curl -fsS -H "Authorization: Bearer $runtime_key" -H 'Content-Type: application/json' \
  "http://$RUNTIME_CONTAINER:8097/runs/$session_id/goal" \
  -d '{"brief":"Call exec_command to run nproc without requesting escalated permissions. Report the visible CPU count with CODEX_RUNTIME_OK.","scopes":["code"]}' >/dev/null
for _ in {1..180}; do
  steps="$(runtime_curl -fsS -H "Authorization: Bearer $runtime_key" "http://$RUNTIME_CONTAINER:8097/runs/$session_id/steps?after=0")"
  [[ "$(jq -r .done <<<"$steps")" == true ]] && break
  sleep 1
done
[[ "$(jq -r .failed <<<"$steps")" == false ]]
[[ "$(jq -r '[.steps[] | select(.phase == "observation") | .summary] | join("\n")' <<<"$steps")" == *"2"* ]]
artifact="$(runtime_curl -fsS -H "Authorization: Bearer $runtime_key" "http://$RUNTIME_CONTAINER:8097/runs/$session_id/artifacts")"
jq -e '.artifacts[0].text_md | contains("CODEX_RUNTIME_OK")' <<<"$artifact" >/dev/null
echo "Lab Codex Runtime end-to-end probe passed"
