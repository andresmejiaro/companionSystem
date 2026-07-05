"""Local HTTP server exposing the assistant REPL to a browser UI.

Wraps one `AssistantSession` (see `profile_os.openai_assistant`) behind a
small FastAPI app and serves the static frontend in `web/`. The browser
never sees a secret: OPENAI_API_KEY and PROFILE_OS_BRIDGE_BEARER stay in
this process's env — the frontend only speaks the /api/* JSON endpoints.

Single-session by design: one profile, one conversation, in memory. This
is a local MVP, not a product — no auth, no multi-user, no streaming, no
persistence of the chat itself (memory persistence is the backend's job,
reached through the existing ToolBridge tool loop).

API:
    GET  /              the chat UI (web/index.html; /static/* for assets)
    POST /api/start     boot the profile (idempotent: re-POST is a no-op)
    POST /api/message   {"text": ...} -> {"reply", "tool_events"}
    POST /api/closeout  {"notes": optional} -> closeout result
    GET  /api/status    session state (profile, started, turns, ...)

Run against a live local backend:

    OPENAI_API_KEY=... PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET \
        python -m profile_os.assistant_server --profile tara --port 8787

Env: same as `profile_os.openai_assistant` (OPENAI_API_KEY,
OPENAI_SMOKE_MODEL, PROFILE_OS_BRIDGE_BASE_URL, PROFILE_OS_BRIDGE_BEARER).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .bridge import ToolBridge, ToolBridgeError
from .openai_assistant import AssistantSession
from .openai_smoke import DEFAULT_MODEL

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class MessageIn(BaseModel):
    text: str


class CloseoutIn(BaseModel):
    notes: str | None = None


class _State:
    """One session + a per-request tool-event buffer for the debug panel."""

    def __init__(self, profile_id: str, model: str,
                 bridge: ToolBridge | None = None, client=None):
        self.tool_events: list[str] = []
        self.session = AssistantSession(
            bridge or ToolBridge(), profile_id, model, client,
            printer=lambda line: self.tool_events.append(str(line)))
        self.boot: dict | None = None
        self.closed = False


def create_assistant_app(profile_id: str = "tara",
                         model: str = DEFAULT_MODEL,
                         bridge: ToolBridge | None = None,
                         client=None) -> FastAPI:
    """App factory; `bridge`/`client` injection is for tests."""
    app = FastAPI(title="Profile OS assistant server")
    state = _State(profile_id, model, bridge, client)
    app.state.assistant = state

    def _guard_started():
        if state.boot is None:
            raise HTTPException(409, "session not started; POST /api/start")
        if state.closed:
            raise HTTPException(409, "session already closed out")

    def _bridge_http_error(e: ToolBridgeError) -> HTTPException:
        return HTTPException(502, f"backend error {e.status_code}: {e.detail}")

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.post("/api/start")
    def start():
        if state.closed:
            raise HTTPException(409, "session already closed out")
        started_now = state.boot is None
        if started_now:
            try:
                state.boot = state.session.start()
            except ToolBridgeError as e:
                raise _bridge_http_error(e)
        boot = state.boot
        return {"started_now": started_now,
                "profile_id": profile_id,
                "display_name": boot.get("profile", {}).get(
                    "display_name", profile_id),
                "compact_state": boot.get("compact_state", ""),
                "recent_memories": len(boot.get("recent_memories", []))}

    @app.post("/api/message")
    def message(body: MessageIn):
        _guard_started()
        state.tool_events.clear()
        try:
            reply = state.session.send(body.text)
        except ToolBridgeError as e:
            raise _bridge_http_error(e)
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        return {"reply": reply, "tool_events": list(state.tool_events)}

    @app.post("/api/closeout")
    def closeout(body: CloseoutIn):
        _guard_started()
        try:
            result = state.session.close(body.notes or None)
        except ToolBridgeError as e:
            raise _bridge_http_error(e)
        state.closed = True
        return {"closed": True, "result": result}

    @app.get("/api/status")
    def status():
        return {"profile_id": profile_id,
                "model": model,
                "started": state.boot is not None,
                "closed": state.closed,
                "user_turns": state.session.user_turns}

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Local web UI server for the assistant REPL.")
    ap.add_argument("--profile", default="tara")
    ap.add_argument("--model",
                    default=os.environ.get("OPENAI_SMOKE_MODEL",
                                           DEFAULT_MODEL))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args(argv)

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 1

    import uvicorn
    app = create_assistant_app(args.profile, args.model)
    print(f"assistant UI: http://{args.host}:{args.port}/ "
          f"(profile {args.profile!r})")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
