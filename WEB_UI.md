# Web UI

A boring, mobile-packable browser UI for the local assistant MVP. Two
pieces:

- `profile_os/assistant_server.py` — local FastAPI server that owns the
  secrets and one `AssistantSession` (the same tool loop the REPL uses).
- `web/` — a static frontend (plain HTML/CSS/JS, no build step) served
  by that server.

```
browser (web/)  →  assistant_server (/api/*)  →  AssistantSession /
run_tool_loop  →  ToolBridge  →  backend (profile_os.api)
```

## Running

1. Start the backend (memory source of truth):

   ```sh
   .venv/bin/uvicorn profile_os.api:app --port 8000
   # with auth: PROFILE_OS_AUTH_ENABLED=1 + a bridge bearer, see ACCESS_CONTROL.md
   ```

2. Start the assistant server:

   ```sh
   OPENAI_API_KEY=... PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET \
       python -m profile_os.assistant_server --profile tara --port 8787
   ```

3. Open the UI: <http://127.0.0.1:8787/>

The page boots the profile via `POST /api/start`, sends turns via
`POST /api/message`, shows tool calls in the collapsible "Tool / debug
log" panel, and the **Close out** button ends the session via
`POST /api/closeout` (updates the profile's compact state).

## Why secrets are server-side

`OPENAI_API_KEY` and `PROFILE_OS_BRIDGE_BEARER` live only in the
assistant server's environment. The browser gets static files and JSON
replies — never a credential. Anything shipped to a browser (or later a
mobile app bundle) must be treated as public, so the frontend has zero
secrets and zero backend logic; it only calls `/api/*`.

## Going mobile later

`web/` is deliberately static and self-contained:

- All backend access goes through the small `api` client in
  `web/app.js`, driven by one `API_BASE` constant. It defaults to
  same-origin; a mobile shell sets `window.API_BASE = "http://host:8787"`
  before `app.js` loads.
- No build step, no framework, mobile-first layout.

So packaging options are mechanical: wrap `web/` with **Capacitor**
(copy the folder in as the web assets, point `API_BASE` at the server)
or embed it in a native **WebView**. The server API stays identical.

## API

| Route | Purpose |
|---|---|
| `GET /` | serves `web/index.html` (assets under `/static/*`) |
| `POST /api/start` | boot the profile; idempotent |
| `POST /api/message` | `{"text": ...}` → `{"reply", "tool_events"}` |
| `POST /api/closeout` | `{"notes": optional}` → closeout result |
| `GET /api/status` | profile, started/closed, user turn count |

## Current limitations

- Local only — no deployment story, bind to localhost.
- Single session, single profile per server process; restart for a new
  session after closeout.
- No production auth on the assistant server itself.
- No streaming — each reply arrives when the tool loop finishes.
- Same operational-only tool allowlist as the REPL: no store writes
  (`propose_store`/`add_record`) and no admin lifecycle tools.
