# Remote MCP connector for Claude.ai

Profile OS can run as a remote MCP server for Claude.ai custom connectors.
The MCP service is only an adapter: it authenticates Claude-facing requests,
logs MCP tool calls, and forwards operations to the existing Profile OS
backend with its own backend credential from env.

## Transport and tools

- MCP endpoint: `POST /mcp` and `GET /mcp` on one Streamable HTTP endpoint.
- Health endpoint: `GET /health`.
- OAuth metadata:
  - `GET /.well-known/oauth-protected-resource`
  - `GET /.well-known/oauth-authorization-server`
  - `POST /oauth/register`
  - `GET /oauth/authorize`
  - `POST /oauth/token`

Exposed MCP tools:

- `whoami`
- `start_session`
- `propose_prompt_edit`
- `list_profiles`
- `boot_profile`
- `remember`
- `update_memory`
- `forget`
- `send_message`
- `read_inbox`
- `mark_message_read`
- `write_file`
- `list_files`
- `read_file`
- `delete_file`
- `search_memories`
- `closeout`
- `list_stores`
- `propose_store`
- `query_records`
- `add_record`

Not exposed as MCP tools: approve, reject, archive, audit, profile
management, grant management, or credential management.

## Docker setup

Create `.env` from the template:

```bash
cp .env.example .env
```

Replace every `change-me-*` value. Generate long random values locally, for
example:

```bash
python3 - <<'PY'
import secrets
for name in ("PROFILE_OS_MCP_BACKEND_BEARER", "MCP_CONNECTOR_TOKEN", "MCP_OAUTH_SIGNING_KEY"):
    print(f"{name}={secrets.token_urlsafe(48)}")
PY
```

Run backend + MCP:

```bash
docker compose up --build
```

What compose does:

- persists Profile OS data in the `profile-os-data` Docker volume
- starts the backend with `PROFILE_OS_AUTH_ENABLED=1`
- bootstraps a `bridge` principal with operational grants only
- starts the MCP service on port `8080`
- points the MCP service at `http://backend:8000`
- injects `PROFILE_OS_MCP_BACKEND_BEARER` only into backend/MCP runtime env

Health checks:

```bash
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8080/health
```

## HTTPS exposure

Claude.ai custom connectors need a reachable HTTPS URL. For local testing use
a tunnel; for production use a stable HTTPS deployment or a named tunnel.

Examples:

```bash
cloudflared tunnel --url http://127.0.0.1:8080
ngrok http 8080
```

Set `MCP_PUBLIC_BASE_URL` in `.env` to the final HTTPS origin, with no trailing
slash, then restart the MCP service:

```bash
docker compose up -d --force-recreate mcp
```

This matters for OAuth audience validation: issued access tokens are scoped to
`$MCP_PUBLIC_BASE_URL/mcp`.

## Claude.ai custom connector setup

1. Start the Docker stack and expose the MCP service over HTTPS.
2. In Claude.ai, open custom connector setup for the Project/account.
3. Add a connector named `Profile OS`.
4. Server URL: `https://your-public-origin.example/mcp`.
5. Prefer OAuth if Claude offers it. The server publishes protected-resource
   metadata, authorization-server metadata, dynamic client registration, and
   Authorization Code + PKCE token exchange.
6. If the Claude UI only offers bearer-token auth, use `MCP_CONNECTOR_TOKEN`.
   Do not use `PROFILE_OS_MCP_BACKEND_BEARER`; that secret is only for the MCP
   server to call the Profile OS backend.
7. Add the connector to a Claude Project with no companion identity/system
   prompt.
8. Say: `Boot Sidra.`
9. Expected first tool call: `boot_profile` with `{"profile_id": "sidra"}`.
10. Claude should then answer using the returned `base_prompt`, `role_prompt`,
    `compact_state`, profile metadata, memory policy, closeout rules, and
    allowed tools.

After boot, Claude can call `remember`, `search_memories`, `closeout`,
`list_stores`, `propose_store`, `query_records`, and `add_record`. Record
writes still require a backend-approved dynamic store; pending/rejected/
archived stores are rejected by the backend.

## Local MCP smoke

With `MCP_CONNECTOR_TOKEN` set in `.env`:

```bash
curl -sS -X POST http://127.0.0.1:8080/mcp \
  -H "Authorization: Bearer $MCP_CONNECTOR_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Boot Sidra:

```bash
curl -sS -X POST http://127.0.0.1:8080/mcp \
  -H "Authorization: Bearer $MCP_CONNECTOR_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"boot_profile","arguments":{"profile_id":"sidra"}}}'
```

## Auth details

`MCP_AUTH_REQUIRED=1` should stay enabled for any remote server.

The MCP service accepts either:

- a static connector bearer from `MCP_CONNECTOR_TOKEN` or `MCP_CONNECTOR_TOKENS`
- an OAuth access token issued by this MCP service

OAuth is intentionally minimal and single-operator oriented:

- dynamic client registration validates redirect hosts against
  `MCP_OAUTH_ALLOWED_REDIRECT_HOSTS`
- authorization uses code flow with PKCE S256
- `GET /oauth/authorize` shows a login form (admin secret + live TOTP code
  from an authenticator app) instead of auto-issuing a code — dynamic
  client registration is open by design, so this is the actual gate
  against a stranger self-authorizing. See ACCESS_CONTROL.md "OAuth
  authorize consent screen." Enroll with `python -m profile_os.enroll_totp`.
- access tokens are signed locally with `MCP_OAUTH_SIGNING_KEY`
- tokens include issuer, audience, scope, issued-at, expiry, and client id
- `/mcp` validates bearer token expiry, scope, issuer, and audience
- this only happens once per connector, not per message: tokens last
  `MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS` and there's no refresh grant, so
  re-authorizing after expiry means the TOTP screen again
- registered clients (from `POST /oauth/register`) persist to
  `MCP_OAUTH_STATE_FILE` on a Docker volume, so a redeploy (which recreates
  the mcp container) doesn't invalidate a connector that already completed
  registration — without this, ChatGPT/Claude would need to reconnect from
  scratch after every `docker compose up --build`

## Origin handling and logging

For browser-origin requests, `/mcp` validates `Origin` against
`MCP_ALLOWED_ORIGINS`. Requests without an Origin header are allowed for
server-to-server MCP clients. Localhost origins are allowed only when no
explicit allowlist is configured.

Every MCP tool call logs one structured line through Python logging with tool
name, profile id when present, outcome, backend status on backend errors, and
elapsed time. Tool arguments and secrets are not logged.

## Security boundaries

- Backend remains the source of truth for profiles, prompts, compact state,
  memory, dynamic stores, and record validation.
- No companion identity is hardcoded in the MCP server.
- Claude-facing tokens are never forwarded to the backend.
- Backend bearer credentials are read only from env.
- No secrets belong in prompts, tool descriptions, repo files, or static web
  assets.
- Admin approve/reject/archive actions are not MCP tools.
- Existing local browser UI still talks to the original backend routes.

References:

- MCP Streamable HTTP transport: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- MCP authorization: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- Anthropic MCP connector docs: https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector
