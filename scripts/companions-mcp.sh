#!/usr/bin/env bash
# Companions MCP bridge for stock CLI agents (Claude Code, Codex).
#
# Spawned once per agent session by the client, it forwards to the remote
# companions MCP through mcp-remote and stamps a per-spawn conversation id into
# the x-conv-id header. That id is the session-binding fingerprint the server
# uses to lock a conversation to one companion (see SESSION_BINDING_PLAN.md).
#
# Why a wrapper instead of the client's own session id: Claude Code exposes
# CLAUDE_CODE_SESSION_ID, but Codex scrubs the environment for MCP children
# (verified: children get only HOME/LANG/LOGNAME/PATH/SHELL/USER), so no native
# id reaches the server. Generating the id here works on both, identically.
#
# The id is fresh per spawn: concurrent sessions are isolated; a resumed session
# gets a new id (looks like a new conversation -> rebinds, harmless). To make a
# session resume-stable, export COMPANIONS_CONV_ID before launching the client.
#
# Register:
#   Codex:       codex mcp add companions -- /path/to/companions-mcp.sh
#   Claude Code: "companions": {"command": "/path/to/companions-mcp.sh"} in .mcp.json
set -euo pipefail
URL="${COMPANIONS_MCP_URL:-https://rumbo.datacodemath.com/mcp}"
CID="${COMPANIONS_CONV_ID:-$(cat /proc/sys/kernel/random/uuid 2>/dev/null \
      || python3 -c 'import uuid;print(uuid.uuid4())')}"
exec npx -y mcp-remote "$URL" --header "x-conv-id:${CID}"
