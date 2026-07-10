# Tool bridge (MCP-shaped, HTTP transport)

`profile_os/bridge.py` is a thin tool bridge that lets an external hosted
assistant (Claude.ai / ChatGPT / Gemini) operate an Assistant Profile
through named tools. It is a pure client of the existing HTTP API — no
business logic, no storage, no LLM calls; the backend stays the source of
truth and does **all** authorization.

## Bridge and remote MCP

The bridge remains the reusable HTTP client for local runners and the remote
MCP server. `profile_os/mcp_server.py` now wraps it as a Streamable HTTP MCP
server for Claude.ai custom connectors; see [MCP_CONNECTOR.md](MCP_CONNECTOR.md)
for deployment and connector setup.

The original bridge shape is still useful:

- `bridge.TOOLS` — a list of `{name, description, inputSchema, outputSchema}`
  dicts, which local runners use as hosted-assistant tool definitions.
- `ToolBridge.call(name, arguments)` — generic dispatch for the older local
  harness and smoke runners.

The remote MCP server deliberately exposes a narrower, Claude-facing tool set:
`list_profiles`, `boot_profile`, `remember`, `search_memories`, `closeout`,
`list_stores`, `propose_store`, `query_records`, and `add_record`.

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

# use the bridge from Python (the remote MCP server uses the same backend calls)
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

## Local harness (integration rehearsal)

`profile_os/harness.py` simulates a hosted assistant driving the bridge —
it tests the tool wiring end-to-end **without any deployment**. It is not
a chatbot and calls no LLM: it runs a fixed scripted flow (boot → remember
→ search → propose store → expected 409 on a pre-approval write → audit →
closeout), printing each tool call and a final summary.

```bash
# against a running local backend (same env vars as the bridge)
PROFILE_OS_BRIDGE_BASE_URL=http://127.0.0.1:8000 \
PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET \
python -m profile_os.harness --profile tara
```

With auth enabled the harness needs a bridge credential holding the
operational grants (see "Tara-only bridge" above); it never bypasses auth
and never approves schemas — 401/403 abort the run with a clear hint, and
the proposed store is deliberately left `pending` for an admin.

In tests the same flow runs in-process with no network:
`run_harness(ToolBridge(client=TestClient(app)))` — see
`tests/test_harness.py`.

Connecting a real hosted Claude/ChatGPT is a later step: those platforms
need a publicly reachable HTTPS bridge (real MCP server plus a tunnel or
deployment). This harness rehearses exactly the tool sequence such an
assistant would perform, locally.

## Local real-model runners (smoke + REPL)

Two optional dev tools drive the bridge with an **actual OpenAI model**
deciding the tool calls — still local-only: no server, no deployment, not
public, and not production auth (they use the same bridge bearer from env
as everything else). The `openai` package is lazily imported, so nothing
else in the project (or its tests) needs it installed.

```bash
# one-shot smoke run
OPENAI_API_KEY=... PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET \
python -m profile_os.openai_smoke --profile tara "remember I tested this"

# interactive assistant REPL (boots the profile once, keeps history,
# /exit triggers a closeout)
OPENAI_API_KEY=... PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET \
python -m profile_os.openai_assistant --profile tara
```

Both expose only operational tools (boot, remember, search_memories,
list_stores, get_store, query_records, audit, closeout) — no lifecycle
tools, and no propose_store/add_record in this slice; a model request for
anything else is refused without executing. Secrets stay in env and are
never printed; backend 401/403 abort with a clear message. Fake-client
tests live in `tests/test_openai_assistant.py`.

## Current limitations

- The bridge itself is a Python-process client. Hosted platforms should use
  the standalone remote MCP server in `profile_os/mcp_server.py`.
- Synchronous `httpx.Client`; fine for a single-assistant bridge.
- No streaming, no pagination beyond the backend's `limit` params.
- No secret rotation helper — revoke and re-issue via `AccessControl`.
