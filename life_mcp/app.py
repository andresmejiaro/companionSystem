from __future__ import annotations

import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jsonschema import ValidationError, validate

from . import __version__
from .config import Settings
from .security import MCPAuth, origin_allowed
from .snapshot import SnapshotError, SnapshotNotFound, SnapshotStore
from .state import JsonState
from .tools import Tool, build_tools


LOGGER = logging.getLogger("life.mcp")
MAX_BODY_BYTES = 1024 * 1024


def _jsonrpc_result(request_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _jsonrpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": code, "message": message},
    })


def _tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}], "isError": is_error,
    }
    if isinstance(value, dict):
        result["structuredContent"] = value
    return result


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if missing := settings.validate():
        LOGGER.warning("configuration incomplete: %s", ", ".join(missing))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = SnapshotStore(settings.data_dir, read_only=settings.read_only)
    state = JsonState(settings.data_dir / "oauth-state.json", {"clients": {}, "codes": {}})
    auth = MCPAuth(settings, state)
    tools = build_tools(store)
    app = FastAPI(title="Life Career Truth MCP", version=__version__)
    app.state.settings = settings
    app.state.snapshot_store = store
    app.state.tools = tools

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        origin = request.headers.get("origin")
        if origin and origin_allowed(origin, settings.allowed_origins):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, MCP-Protocol-Version"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    @app.get("/health")
    async def health():
        status = store.status()
        return {
            "status": "healthy", "service": "life-career-truth-mcp",
            "version": __version__, "snapshot_published": status["published"],
            "read_only": settings.read_only,
        }

    @app.get("/")
    async def root():
        return {
            "service": "life-career-truth-mcp", "mcp_endpoint": "/mcp",
            "mode": "read-only", "publication_locked": settings.read_only,
            "source": store.status(),
        }

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def protected_resource():
        return {
            "resource": settings.resource_url,
            "authorization_servers": [settings.public_base_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp"],
        }

    @app.get("/.well-known/oauth-authorization-server")
    async def authorization_server():
        base = settings.public_base_url
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp"],
        }

    @app.post("/oauth/register", status_code=201)
    async def oauth_register(request: Request):
        try:
            body = await request.json()
            return auth.register(
                body.get("redirect_uris", []),
                str(body.get("client_name") or "MCP client"),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({
                "error": "invalid_client_metadata", "error_description": str(exc),
            }, status_code=400)

    @app.get("/oauth/authorize", response_class=HTMLResponse)
    async def oauth_authorize(request: Request):
        params = dict(request.query_params)
        validated, error = auth.validate_authorize(params)
        if error:
            return HTMLResponse(auth.authorize_html(params, error), status_code=400)
        return HTMLResponse(auth.authorize_html(validated or params))

    @app.post("/oauth/authorize", response_class=HTMLResponse)
    async def oauth_authorize_post(request: Request):
        form = dict(await request.form())
        admin_secret = str(form.pop("admin_secret", ""))
        validated, error = auth.validate_authorize(form)
        if error:
            return HTMLResponse(auth.authorize_html(form, error), status_code=400)
        if not settings.admin_secret or not hmac.compare_digest(
            admin_secret, settings.admin_secret
        ):
            return HTMLResponse(
                auth.authorize_html(validated or form, "Invalid admin secret."),
                status_code=401,
            )
        code = auth.create_code(validated or {})
        query = {"code": code}
        if validated and validated.get("state"):
            query["state"] = validated["state"]
        return RedirectResponse(
            f"{validated['redirect_uri']}?{urlencode(query)}", status_code=303
        )

    @app.post("/oauth/token")
    async def oauth_token(request: Request):
        data = dict(await request.form())
        if data.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        try:
            return auth.exchange_code(data)
        except ValueError as exc:
            return JSONResponse({
                "error": "invalid_grant", "error_description": str(exc),
            }, status_code=400)

    @app.options("/mcp")
    async def mcp_options(request: Request):
        origin = request.headers.get("origin")
        if origin and not origin_allowed(origin, settings.allowed_origins):
            return JSONResponse({"error": "origin_not_allowed"}, status_code=403)
        return Response(status_code=204)

    @app.api_route("/mcp", methods=["GET", "POST"])
    async def mcp(request: Request):
        origin = request.headers.get("origin")
        if origin and not origin_allowed(origin, settings.allowed_origins):
            return JSONResponse({"error": "origin_not_allowed"}, status_code=403)
        if not auth.authenticated(request.headers.get("authorization")):
            metadata = f"{settings.public_base_url}/.well-known/oauth-protected-resource"
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'},
            )
        if request.method == "GET":
            return Response(": life-career-truth-mcp connected\n\n", media_type="text/event-stream")
        try:
            raw = await request.body()
            if len(raw) > MAX_BODY_BYTES:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _jsonrpc_error(None, -32700, "Parse error")
        request_id = message.get("id")
        method = message.get("method")
        if method == "initialize":
            return _jsonrpc_result(request_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "life-career-truth-mcp", "version": __version__},
                "instructions": (
                    "Read-only access to the latest career truth snapshot published "
                    "from the local life workspace. Check career_source_status before "
                    "treating time-sensitive facts as current. No mutation path exists."
                ),
            })
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return Response(status_code=202)
        if method == "ping":
            return _jsonrpc_result(request_id, {})
        if method == "tools/list":
            return _jsonrpc_result(
                request_id, {"tools": [tool.advertised() for tool in tools.values()]}
            )
        if method != "tools/call":
            return _jsonrpc_error(request_id, -32601, "Method not found")
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool: Tool | None = tools.get(name)
        if not tool:
            return _jsonrpc_error(request_id, -32602, "Unknown tool")
        started = time.monotonic()
        try:
            validate(instance=arguments, schema=tool.schema)
            value = tool.handler(arguments)
            LOGGER.info(
                "mcp_tool_call name=%s outcome=ok elapsed_ms=%d", name,
                int((time.monotonic() - started) * 1000),
            )
            return _jsonrpc_result(request_id, _tool_result(value))
        except ValidationError as exc:
            return _jsonrpc_result(request_id, _tool_result({
                "error": {"code": "invalid_arguments", "message": exc.message},
            }, is_error=True))
        except SnapshotNotFound as exc:
            return _jsonrpc_result(request_id, _tool_result({
                "error": {"code": "not_found", "message": str(exc)},
            }, is_error=True))
        except SnapshotError:
            LOGGER.exception("published snapshot failure")
            return _jsonrpc_result(request_id, _tool_result({
                "error": {"code": "snapshot_unavailable", "message": "Published snapshot is unavailable."},
            }, is_error=True))
        except Exception:
            LOGGER.exception("mcp_tool_call name=%s outcome=internal_error", name)
            return _jsonrpc_result(request_id, _tool_result({
                "error": {"code": "internal_error", "message": "Internal server error."},
            }, is_error=True))

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
