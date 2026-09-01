#!/usr/bin/env bash
set -euo pipefail

# Deploy frontend to Cloudflare Workers
# Defaults target the live Simverse Robinhood Chain deployment. Override any
# variable in the environment when deploying a preview or replacement proxy.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/../../frontend"

echo "==> Building frontend..."
cd "$FRONTEND_DIR"
npm ci
VITE_API_URL="${VITE_API_URL:-https://simverse-api.proxypool.eu.org}" \
VITE_WEB3_CHAIN_ID="${VITE_WEB3_CHAIN_ID:-4663}" \
VITE_WEB3_CHAIN_NAME="${VITE_WEB3_CHAIN_NAME:-Robinhood Chain}" \
VITE_WEB3_RPC_URL="${VITE_WEB3_RPC_URL:-https://rpc.mainnet.chain.robinhood.com}" \
VITE_AGENT_REGISTRY_ADDRESS="${VITE_AGENT_REGISTRY_ADDRESS:-0x24f6f6bE48066cbE0B54d741cd4B52862Bb4b05c}" \
npm run build

echo "==> Copying dist to deploy dir..."
rm -rf "$SCRIPT_DIR/dist"
cp -r "$FRONTEND_DIR/dist" "$SCRIPT_DIR/dist"

echo "==> Deploying to Cloudflare Workers..."
cd "$SCRIPT_DIR"
npx wrangler deploy

echo "==> Done!"
