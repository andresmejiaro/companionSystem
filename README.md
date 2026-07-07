# Assistant Profile OS — slice zero

Backend foundation for versioned, provider-agnostic Assistant Profiles
(configuration bundles: prompts, compact state, durable memory, domain data).
Profiles live **here**; Claude/GPT/Gemini UIs are future secondary surfaces.

No LLM is embedded or called. `profile_os/adapters.py` ships a deterministic
`FakeModelAdapter`; real provider adapters are a later slice.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn profile_os.api:app --reload
# → http://127.0.0.1:8000/docs  (OpenAPI UI)
```

First start seeds two example profiles (`sidra`, `tara`) into `./data/`.

## Optional auth (all endpoints)

Auth is **disabled by default** — everything works locally with no keys, as
before. To require credentials on every endpoint except `/health` and
`/demo`:

```bash
# 1. bootstrap an admin credential (local CLI, never over HTTP; stores a hash only)
.venv/bin/python -m profile_os.bootstrap_admin --data-dir data --secret "$SECRET"
# 2. start the server with enforcement on
PROFILE_OS_AUTH_ENABLED=1 .venv/bin/uvicorn profile_os.api:app
# 3. call lifecycle endpoints with the bearer secret
curl -X POST -H "Authorization: Bearer $SECRET" \
  http://127.0.0.1:8000/profiles/tara/stores/<name>/approve
```

Without a valid credential, protected endpoints return 401; with a
credential but no grant for the route's operation on that profile, 403.
`GET /profiles` returns only profiles the principal holds any grant on
(a `*` wildcard grant sees all). The full route → operation map is in
[ACCESS_CONTROL.md](ACCESS_CONTROL.md). The /demo page has an optional API
key field for this.
Credentials belong to principals (apps, bridges, humans, admins), never to
profiles — see [ACCESS_CONTROL.md](ACCESS_CONTROL.md).

## Tool bridge (for external hosted assistants)

`profile_os/bridge.py` exposes the operational endpoints as named tools
(boot, remember, search_memories, closeout, propose_store, list_stores,
get_store, add_record, query_records, audit) for Claude/ChatGPT/Gemini-hosted
assistants. It is a pure HTTP client — the backend keeps doing all
authorization — configured via `PROFILE_OS_BRIDGE_BASE_URL` and
`PROFILE_OS_BRIDGE_BEARER`. See [TOOL_BRIDGE.md](TOOL_BRIDGE.md), including
how local runners reuse the same bridge.

## Remote MCP server (Claude.ai custom connectors)

`profile_os/mcp_server.py` is a deployable remote MCP server over Streamable
HTTP:

- `POST /mcp` and `GET /mcp` on one endpoint
- MCP tools: `list_profiles`, `boot_profile`, `remember`, `search_memories`,
  `closeout`, `list_stores`, `propose_store`, `query_records`, `add_record`
- Claude-facing auth handled by the MCP service; backend auth handled by a
  separate bridge credential from env
- no admin approve/reject/archive tools exposed

Run backend + MCP locally with Docker:

```bash
cp .env.example .env
# edit .env and replace every change-me value
docker compose up --build
```

See [MCP_CONNECTOR.md](MCP_CONNECTOR.md) for OAuth, tunnel/deployment notes,
and the Claude.ai custom connector setup flow.

For a real-model local test (not hosted, not public, not production auth),
`python -m profile_os.openai_assistant --profile tara` runs an interactive
assistant REPL over the bridge, and `python -m profile_os.openai_smoke`
runs a one-shot version; both need `OPENAI_API_KEY` (and the bridge bearer
if auth is enabled) in env. Details in TOOL_BRIDGE.md.

## Demo console

With the server running, open **http://127.0.0.1:8000/demo** — a single static
HTML page (no framework, no build step) for inspecting and exercising the
backend by hand.

2-minute demo script:

1. **Profiles** — click "List profiles"; pick `tara` in the dropdown.
2. **Boot** — click `boot(profile)`: compact state, collapsed base/role
   prompts, recent memories. "Copy boot bundle" puts a paste-anywhere
   plaintext bundle on the clipboard.
3. **Memory** — remember a note ("tried the demo"), then search for "demo".
4. **Closeout** — enter a new_state ("Demo day logged."), submit, click
   `boot()` again: the compact state changed. (The backend never summarizes —
   you supplied that state.)
5. **Tara flow** — click "Run Tara hotel demo": proposes
   `hotel_reservations`, approves it, adds a reservation, queries it, and
   shows the audit trail, printing each HTTP step.
6. **Validation** — in section 5, click "add INVALID record": the backend
   rejects unknown fields and the impossible date `2026-02-30` with a 422.

What the demo is intentionally **not**: the final UI (see UI_SPEC.md), a
chatbot, an LLM integration, or a polished admin panel. Approve/reject/
archive (and every other endpoint) are enforced when
`PROFILE_OS_AUTH_ENABLED=1`; the default local mode stays open.
Override the data directory with `PROFILE_OS_DATA_DIR=/path`.

Everything is inspectable on disk:

- `data/profile_os.db` — SQLite (registry, memory events, compact state, domain records, closeouts)
- `data/profiles/<id>/base_prompt.md`, `role_prompt.md` — plain markdown prompts
- `data/profiles/<id>/closeouts.jsonl` — append-only closeout log

Slice two adds **dynamic stores**: profiles propose durable structured data
stores (name + purpose + field schema); the user/admin approves, rejects, or
archives them; the backend validates every record against the approved schema
and audits all lifecycle changes. The platform never hardcodes what a profile
may store. All endpoints are grant-protected when auth is enabled — see
ACCESS_CONTROL.md.

Note on closeout: the backend does not summarize sessions. The caller supplies
the new compact state (`new_state`); the backend validates and stores it
verbatim. This keeps the backend free of LLM calls.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

Local only; no network, no API keys, no LLM calls.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — reuse/build decision, design, security plan
- [API.md](API.md) — HTTP API / tool contract
- [UI_SPEC.md](UI_SPEC.md) — spec for the later web/mobile client

## Docker

```bash
docker build -t profile-os . && docker run -p 8000:8000 -v $PWD/data:/app/data profile-os
```

For the deployable backend + MCP stack, use `docker compose up --build`.
