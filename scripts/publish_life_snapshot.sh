#!/usr/bin/env bash
set -euo pipefail

SNAPSHOT_PATH="${1:-/home/andres/life/artifacts/jobdocs/single_source_of_truth.json}"
DEPLOY_HOST="${LIFE_MCP_DEPLOY_HOST:-root@62.238.55.207}"
DEPLOY_DIR="${LIFE_MCP_DEPLOY_DIR:-/opt/profile-os}"

if [[ ! -f "$SNAPSHOT_PATH" ]]; then
  echo "snapshot not found: $SNAPSHOT_PATH" >&2
  exit 1
fi

.venv/bin/python -m life_mcp.publish_snapshot --validate-only "$SNAPSHOT_PATH"
ssh "$DEPLOY_HOST" \
  "cd '$DEPLOY_DIR' && docker compose exec -T life-mcp python -m life_mcp.publish_snapshot --stdin" \
  < "$SNAPSHOT_PATH"
