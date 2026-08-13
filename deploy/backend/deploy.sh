#!/usr/bin/env bash
set -euo pipefail

# Deploy backend to remote server via SSH + Docker Compose
# Usage: ./deploy.sh [user@host]

REMOTE="${1:-user@your-server-ip}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/../.."
BACKEND_DIR="$PROJECT_DIR/backend"
REMOTE_DIR="/opt/skills-world"

echo "==> Syncing backend to $REMOTE:$REMOTE_DIR..."
ssh "$REMOTE" "mkdir -p $REMOTE_DIR/backend $REMOTE_DIR/deploy"

# Sync backend code (exclude local dev artifacts).
# NEVER sync .env: the remote backend/.env may hold live runtime config, and
# --delete without this exclude would overwrite/remove it with the local dev
# copy (2026-08-06 near-incident: would have silently swapped the agent-worker
# LLM provider). Excluded receiver files are also protected from --delete.
rsync -avz --delete \
  --exclude '.env' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '*.pyc' \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude 'tmp/' \
  --exclude '_to_delete/' \
  --exclude 'data/' \
  --exclude '*.db' \
  --exclude '*.db.bak*' \
  --exclude 'static/uploads/' \
  "$BACKEND_DIR/" "$REMOTE:$REMOTE_DIR/backend/"

# Sync deploy configs
rsync -avz \
  "$SCRIPT_DIR/docker-compose.yml" \
  "$SCRIPT_DIR/Dockerfile" \
  "$REMOTE:$REMOTE_DIR/deploy/"

# Refuse to start with an absent live configuration. The template is never
# uploaded as a substitute because it contains placeholders and safe defaults,
# not production credentials or an approved rollout state.
if ! ssh "$REMOTE" "test -f $REMOTE_DIR/deploy/.env"; then
  echo "ERROR: $REMOTE_DIR/deploy/.env is missing on $REMOTE; refusing deployment." >&2
  exit 1
fi

echo "==> Building and starting services on $REMOTE..."
ssh "$REMOTE" "cd $REMOTE_DIR/deploy && docker compose up -d --build --wait --wait-timeout 300"

echo "==> Checking health..."
if ! ssh "$REMOTE" "curl -fsS http://localhost:8100/health >/dev/null"; then
  echo "ERROR: API health check failed; deployment is not complete." >&2
  exit 1
fi
echo " ✓ API healthy"

echo "==> Done!"
