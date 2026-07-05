# Tool bridge (MCP-shaped, HTTP transport)

`profile_os/bridge.py` is a thin tool bridge that lets an external hosted
assistant (Claude.ai / ChatGPT / Gemini) operate an Assistant Profile
through named tools. It is a pure client of the existing HTTP API — no
business logic, no storage, no LLM calls; the backend stays the source of
truth and does **all** authorization.

## Why HTTP bridge first, real MCP later

An MCP server would add a new dependency (`mcp` SDK) for what is, in this
slice, ten 1:1 endpoint wrappers. Instead the bridge is already MCP-shaped
so the later wrap is mechanical:

- `bridge.TOOLS` — a list of `{name, description, inputSchema}` dicts,
  which is exactly MCP's `list_tools` response shape.
- `ToolBridge.call(name, arguments)` — exactly MCP's `call_tool` handler.

A future `profile_os/mcp_server.py` is ~30 lines: instantiate `ToolBridge`
from env, return `TOOLS` from `list_tools`, forward `call_tool` to
`bridge.call`, run over stdio. Nothing else changes.

## Configuration

```bash
PROFILE_OS_BRIDGE_BASE_URL=http://127.0.0.1:8000   # backend base URL (default)
PROFILE_OS_BRIDGE_BEARER=<secret>                  # bridge credential secret
```

If the backend runs with auth disabled (the local default), the bearer can
be omitted. If `PROFILE_OS_AUTH_ENABLED=1`, every bridge call sends
`Authorization: Bearer <secret>`; without a valid secret the backend
answers 401, and 403 when the bridge principal lacks the grant — the
bridge surfaces both as `ToolBridgeError` and never retries around them.

## Run it

```bash
# backend (auth on)
python -m profile_os.bootstrap_admin --data-dir data --secret "$ADMIN_SECRET"
PROFILE_OS_AUTH_ENABLED=1 .venv/bin/uvicorn profile_os.api:app

# use the bridge from Python (the future MCP server calls it the same way)
PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET .venv/bin/python -c '
from profile_os.bridge import ToolBridge
b = ToolBridge()
print(b.call("boot", {"profile_id": "tara"})["profile"]["compact_state"])'
```

## Credential ownership

**The bridge owns the credential, not the model and not a profile** (see
ACCESS_CONTROL.md, scenario 1). The hosted model never sees the secret;
the bridge/runtime injects it per call. Assistant Profiles are resources —
there is no per-profile API key. Creating a bridge credential and grants
is an admin task via the `AccessControl` service (no HTTP grant-management
endpoints exist yet).

### Example: Tara-only bridge

```python
from profile_os.storage import Store
from profile_os.access import AccessControl
access = AccessControl(Store("data"))
p = access.create_principal("bridge", "Claude Tara bridge")
access.create_credential(p["id"], "prod", BRIDGE_SECRET)
for op in ("boot", "remember", "search", "closeout",
           "stores:propose", "records:read", "records:write", "audit:read"):
    access.grant(p["id"], op, profile_id="tara")
```

### Example: shared Tara+Sidra bridge

Same principal and credential; duplicate the operational grants for each
profile (`profile_id="tara"` and `profile_id="sidra"`). One credential,
many profiles — grants are many-to-many.

## Tools exposed

| Tool | Backend route | Required grant |
|---|---|---|
| `boot(profile_id)` | `POST /profiles/{id}/boot` | `boot` |
| `remember(profile_id, kind, content, tags=[])` | `POST /profiles/{id}/memories` | `remember` |
| `search_memories(profile_id, query, limit=20)` | `GET /profiles/{id}/memories/search` | `search` |
| `closeout(profile_id, notes, new_state)` | `POST /profiles/{id}/closeout` | `closeout` |
| `propose_store(profile_id, name, purpose, schema)` | `POST /profiles/{id}/stores` | `stores:propose` |
| `list_stores(profile_id)` | `GET /profiles/{id}/stores` | `records:read` |
| `get_store(profile_id, name)` | `GET /profiles/{id}/stores/{name}` | `records:read` |
| `add_record(profile_id, store_name, data)` | `POST …/stores/{name}/records` | `records:write` |
| `query_records(profile_id, store_name, contains, limit)` | `GET …/stores/{name}/records` | `records:read` |
| `audit(profile_id, store_name=None, limit=100)` | `GET …/audit` | `audit:read` |

**Not exposed:** `approve`/`reject`/`archive` store lifecycle. These are
admin/user decisions requiring `stores:approve`; a bridge credential is
operational by design. An admin uses the demo console or the API directly.
Also not exposed: profile CRUD, grant management, credential management.

## Current limitations

- Python-process client, not a standalone server: the hosted platform
  needs a runtime that can import `profile_os.bridge` (or the future MCP
  server wrapping it).
- Synchronous `httpx.Client`; fine for a single-assistant bridge.
- No streaming, no pagination beyond the backend's `limit` params.
- No secret rotation helper — revoke and re-issue via `AccessControl`.
