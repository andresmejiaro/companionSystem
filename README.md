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

## Optional auth (dynamic-store lifecycle only)

Auth is **disabled by default** — everything works locally with no keys, as
before. To require credentials on store approve/reject/archive:

```bash
# 1. bootstrap an admin credential (local CLI, never over HTTP; stores a hash only)
.venv/bin/python -m profile_os.bootstrap_admin --data-dir data --secret "$SECRET"
# 2. start the server with enforcement on
PROFILE_OS_AUTH_ENABLED=1 .venv/bin/uvicorn profile_os.api:app
# 3. call lifecycle endpoints with the bearer secret
curl -X POST -H "Authorization: Bearer $SECRET" \
  http://127.0.0.1:8000/profiles/tara/stores/<name>/approve
```

Without a valid credential these endpoints return 401; with a credential but
no `stores:approve` grant for that profile, 403. All other endpoints remain
open in this slice. The /demo page has an optional API key field for this.
Credentials belong to principals (apps, bridges, humans, admins), never to
profiles — see [ACCESS_CONTROL.md](ACCESS_CONTROL.md).

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
chatbot, an LLM integration, or a secure admin panel — approve/reject/archive
buttons are labeled "future admin action — NOT SECURE YET" because auth does
not exist yet.
Override the data directory with `PROFILE_OS_DATA_DIR=/path`.

Everything is inspectable on disk:

- `data/profile_os.db` — SQLite (registry, memory events, compact state, domain records, closeouts)
- `data/profiles/<id>/base_prompt.md`, `role_prompt.md` — plain markdown prompts
- `data/profiles/<id>/closeouts.jsonl` — append-only closeout log

Slice two adds **dynamic stores**: profiles propose durable structured data
stores (name + purpose + field schema); the user/admin approves, rejects, or
archives them; the backend validates every record against the approved schema
and audits all lifecycle changes. The platform never hardcodes what a profile
may store. Approval endpoints are not yet auth-protected — see ARCHITECTURE.md.

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
- [API.md](API.md) — HTTP API / tool contract (also mirrored as future MCP tools)
- [UI_SPEC.md](UI_SPEC.md) — spec for the later web/mobile client

## Docker (for the later cloud path — not deployed in this slice)

```bash
docker build -t profile-os . && docker run -p 8000:8000 -v $PWD/data:/app/data profile-os
```
