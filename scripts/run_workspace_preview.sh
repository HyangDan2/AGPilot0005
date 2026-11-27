#!/usr/bin/env bash
# Hardened local helpers: AGCore R2-W Workspace & RAG preview (independent of the R1.3 runtime).
set -euo pipefail
cd "$(dirname "$0")/.."

STATE_DIR="${AGCORE_WORKSPACE_STATE_DIR:-${TMPDIR:-/tmp}/hd2lab-r2w}"
mkdir -p "$STATE_DIR"

export AGCORE_WORKSPACE_ENABLED="${AGCORE_WORKSPACE_ENABLED:-true}"
export AGCORE_WORKSPACE_DB_PATH="${AGCORE_WORKSPACE_DB_PATH:-$STATE_DIR/workspace.db}"
export AGCORE_WORKSPACE_STORAGE_ROOT="${AGCORE_WORKSPACE_STORAGE_ROOT:-$STATE_DIR/storage}"
export AGCORE_WORKSPACE_EMBEDDING_PROVIDER="${AGCORE_WORKSPACE_EMBEDDING_PROVIDER:-local}"
export AGCORE_WORKSPACE_CORS_ORIGINS="${AGCORE_WORKSPACE_CORS_ORIGINS:-http://127.0.0.1:5173,http://127.0.0.1:4173}"

PORT="${AGCORE_WORKSPACE_PREVIEW_PORT:-8210}"
echo "R2-W workspace preview"
echo "  db      : $AGCORE_WORKSPACE_DB_PATH"
echo "  storage : $AGCORE_WORKSPACE_STORAGE_ROOT"
echo "  embedder: $AGCORE_WORKSPACE_EMBEDDING_PROVIDER"
echo "  listen  : http://127.0.0.1:$PORT  (health: /health, mock: /api/v1/workspaces/_mock)"

exec python3 -m uvicorn app.workspace.preview_app:app --host 127.0.0.1 --port "$PORT" --log-level info
