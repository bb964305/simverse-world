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
  --exclude 'data/' \
  --exclude '*.db' \
  --exclude 'static/uploads/' \
  "$BACKEND_DIR/" "$REMOTE:$REMOTE_DIR/backend/"

# Sync deploy configs
rsync -avz \
  "$SCRIPT_DIR/docker-compose.yml" \
  "$SCRIPT_DIR/Dockerfile" \
  "$REMOTE:$REMOTE_DIR/deploy/"

# Check if .env exists on remote, if not copy example
ssh "$REMOTE" "test -f $REMOTE_DIR/deploy/.env || echo 'WARNING: No .env file found. Copy .env.example and fill in values.'"

echo "==> Building and starting services on $REMOTE..."
ssh "$REMOTE" "cd $REMOTE_DIR/deploy && docker compose up -d --build"

echo "==> Checking health..."
sleep 3
ssh "$REMOTE" "curl -sf http://localhost:8100/health && echo ' ✓ API healthy' || echo ' ✗ API not responding'"

echo "==> Done!"
