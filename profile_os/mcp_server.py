"""Remote MCP server for Profile OS over Streamable HTTP.

This process is an adapter, not a second backend. It validates remote MCP
client credentials, exposes Profile OS operations as MCP tools, and then calls
the existing HTTP backend through ToolBridge using a separate backend bearer
from env. Incoming Claude/client tokens are never forwarded to Profile OS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .bridge import ToolBridge, ToolBridgeError

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {MCP_PROTOCOL_VERSION, "2025-03-26"}
SERVER_VERSION = "0.1.0"
SCOPE = "profile-os"

LOGGER = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _canonical_base(url: str) -> str:
    return url.rstrip("/")


def _is_local_origin(origin: str) -> bool:
    parsed = urllib.parse.urlparse(origin)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "localhost", "127.0.0.1", "::1"
    }


def _host_matches(pattern: str, host: str) -> bool:
    pattern = pattern.lower()
    host = host.lower()
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host == suffix or host.endswith("." + suffix)
    return hmac.compare_digest(pattern, host)


def _origin_matches(pattern: str, origin: str) -> bool:
    parsed_origin = urllib.parse.urlparse(origin)
    parsed_pattern = urllib.parse.urlparse(pattern)
    if not parsed_origin.scheme or not parsed_origin.netloc:
        return False
    if not parsed_pattern.scheme or not parsed_pattern.netloc:
        return False
    if parsed_origin.scheme.lower() != parsed_pattern.scheme.lower():
        return False
    if parsed_pattern.port != parsed_origin.port:
        # urlparse returns None for default ports unless explicitly present;
        # keep the comparison strict when a port is configured.
        if parsed_pattern.port is not None:
            return False
    pattern_host = parsed_pattern.hostname or ""
    origin_host = parsed_origin.hostname or ""
    return _host_matches(pattern_host, origin_host)


def _redirect_host_allowed(uri: str, allowed_hosts: list[str]) -> bool:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "https" or not parsed.hostname or parsed.fragment:
        return False
    if not allowed_hosts:
        return True
    return any(_host_matches(pattern, parsed.hostname) for pattern in allowed_hosts)


def _resource_url(settings: "MCPSettings", request: Request) -> str:
    if settings.public_base_url:
        return f"{_canonical_base(settings.public_base_url)}/mcp"
    return str(request.url_for("mcp_endpoint"))


def _issuer_url(settings: "MCPSettings", request: Request) -> str:
    if settings.oauth_issuer:
        return _canonical_base(settings.oauth_issuer)
    if settings.public_base_url:
        return _canonical_base(settings.public_base_url)
    url = urllib.parse.urlparse(str(request.url_for("health")))
    return urllib.parse.urlunparse((url.scheme, url.netloc, "", "", "", ""))


def _metadata_url(settings: "MCPSettings", request: Request) -> str:
    base = settings.public_base_url
    if base:
        return f"{_canonical_base(base)}/.well-known/oauth-protected-resource"
    return str(request.url_for("oauth_protected_resource_metadata"))


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _tool(
    name: str,
    title: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_PROFILE_ID = {
    "type": "string",
    "description": "Profile id to operate on, such as a value returned by list_profiles.",
}

_MEMORY_KINDS = [
    "decision",
    "fact",
    "failure_scar",
    "note",
    "observation",
    "preference",
]

MCP_TOOLS = [
    _tool(
        "whoami",
        "Who Am I",
        "Canonical 'who am I talking to' identity file. Overrides your memory"
        " on conflict — the file wins and drift gets logged by the human."
        " Call this when unsure about personal facts (identity, health,"
        " work, family, key people) rather than trusting your own memory.",
        {},
        [],
    ),
    _tool(
        "list_profiles",
        "List Profiles",
        "List profiles visible to this connector's backend credential.",
        {},
        [],
    ),
    _tool(
        "boot_profile",
        "Boot Profile",
        "Boot a profile by id. Returns base_prompt, role_prompt, compact_state, profile metadata, recent memories, and profile tool rules. Call this before answering as a profile.",
        {"profile_id": _PROFILE_ID},
        ["profile_id"],
    ),
    _tool(
        "remember",
        "Remember",
        "Append a durable memory event for a profile.",
        {
            "profile_id": _PROFILE_ID,
            "kind": {
                "type": "string",
                "enum": _MEMORY_KINDS,
                "description": "Memory kind. Use note when no narrower kind fits.",
            },
            "content": {"type": "string", "description": "Non-empty memory content."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
        },
        ["profile_id", "kind", "content"],
    ),
    _tool(
        "search_memories",
        "Search Memories",
        "Search a profile's memory events by substring over content and tags.",
        {
            "profile_id": _PROFILE_ID,
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        ["profile_id", "query"],
    ),
    _tool(
        "closeout",
        "Close Out",
        "End a session by logging notes and replacing the profile compact_state with the supplied new_state.",
        {
            "profile_id": _PROFILE_ID,
            "notes": {"type": "string", "default": ""},
            "new_state": {"type": "string", "description": "Non-empty compact state."},
        },
        ["profile_id", "new_state"],
    ),
    _tool(
        "list_stores",
        "List Stores",
        "List dynamic store definitions for a profile, including status and schema.",
        {"profile_id": _PROFILE_ID},
        ["profile_id"],
    ),
    _tool(
        "propose_store",
        "Propose Store",
        "Propose a dynamic structured store. Backend approval is required before records can be written.",
        {
            "profile_id": _PROFILE_ID,
            "name": {
                "type": "string",
                "description": "Lowercase store slug, for example hotel_reservations.",
            },
            "purpose": {"type": "string"},
            "schema": {
                "type": "object",
                "description": "Dynamic-store schema: {'fields': {name: {'type': 'string|number|integer|boolean|date', 'required': true|false}}}.",
            },
        },
        ["profile_id", "name", "purpose", "schema"],
    ),
    _tool(
        "query_records",
        "Query Records",
        "Query records in an approved or archived dynamic store.",
        {
            "profile_id": _PROFILE_ID,
            "store_name": {"type": "string"},
            "contains": {"type": "string", "default": ""},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
        ["profile_id", "store_name"],
    ),
    _tool(
        "add_record",
        "Add Record",
        "Add a record to a backend-approved dynamic store. The backend enforces the approved schema and rejects pending, rejected, or archived stores.",
        {
            "profile_id": _PROFILE_ID,
            "store_name": {"type": "string"},
            "data": {"type": "object"},
        },
        ["profile_id", "store_name", "data"],
    ),
]

MCP_TOOL_NAMES = {tool["name"] for tool in MCP_TOOLS}


@dataclass
class MCPSettings:
    auth_required: bool = True
    connector_tokens: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    allow_any_origin: bool = False
    public_base_url: str | None = None
    oauth_issuer: str | None = None
    oauth_signing_key: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    oauth_token_ttl_seconds: int = 60 * 60 * 24 * 30
    oauth_allowed_redirect_hosts: list[str] = field(default_factory=lambda: [
        "claude.ai",
        "*.claude.ai",
    ])

    @classmethod
    def from_env(cls) -> "MCPSettings":
        tokens = _split_csv(os.environ.get("MCP_CONNECTOR_TOKENS"))
        single = os.environ.get("MCP_CONNECTOR_TOKEN")
        if single:
            tokens.append(single)
        ttl_raw = os.environ.get("MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS")
        ttl = int(ttl_raw) if ttl_raw else 60 * 60 * 24 * 30
        redirect_hosts = _split_csv(os.environ.get("MCP_OAUTH_ALLOWED_REDIRECT_HOSTS"))
        if not redirect_hosts:
            redirect_hosts = ["claude.ai", "*.claude.ai"]
        return cls(
            auth_required=_env_bool("MCP_AUTH_REQUIRED", True),
            connector_tokens=tokens,
            allowed_origins=_split_csv(os.environ.get("MCP_ALLOWED_ORIGINS")),
            allow_any_origin=_env_bool("MCP_ALLOW_ANY_ORIGIN", False),
            public_base_url=os.environ.get("MCP_PUBLIC_BASE_URL") or None,
            oauth_issuer=os.environ.get("MCP_OAUTH_ISSUER") or None,
            oauth_signing_key=(
                os.environ.get("MCP_OAUTH_SIGNING_KEY")
                or os.environ.get("MCP_CONNECTOR_TOKEN")
                or secrets.token_urlsafe(32)
            ),
            oauth_token_ttl_seconds=ttl,
            oauth_allowed_redirect_hosts=redirect_hosts,
        )


@dataclass
class OAuthClient:
    client_id: str
    redirect_uris: list[str]
    client_name: str
    issued_at: int


@dataclass
class OAuthCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    resource: str
    expires_at: float


class OAuthState:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients: dict[str, OAuthClient] = {}
        self._codes: dict[str, OAuthCode] = {}

    def register(self, redirect_uris: list[str], client_name: str) -> OAuthClient:
        now = int(time.time())
        client = OAuthClient(
            client_id="posc_" + secrets.token_urlsafe(24),
            redirect_uris=redirect_uris,
            client_name=client_name or "MCP client",
            issued_at=now,
        )
        with self._lock:
            self._clients[client.client_id] = client
        return client

    def get_client(self, client_id: str) -> OAuthClient | None:
        with self._lock:
            return self._clients.get(client_id)

    def create_code(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        resource: str,
    ) -> str:
        code = "poscode_" + secrets.token_urlsafe(32)
        with self._lock:
            self._codes[code] = OAuthCode(
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                resource=resource,
                expires_at=time.time() + 300,
            )
        return code

    def consume_code(self, code: str) -> OAuthCode | None:
        with self._lock:
            item = self._codes.pop(code, None)
        if item is None or item.expires_at < time.time():
            return None
        return item


class MCPToolRunner:
    def __init__(self, bridge: ToolBridge):
        self.bridge = bridge

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "whoami":
            return self.bridge.whoami()
        if name == "list_profiles":
            return self.bridge.list_profiles()
        if name == "boot_profile":
            return self.bridge.boot_profile(arguments["profile_id"])
        if name == "remember":
            return self.bridge.remember(
                arguments["profile_id"],
                arguments["kind"],
                arguments["content"],
                tags=arguments.get("tags") or [],
            )
        if name == "search_memories":
            return self.bridge.search_memories(
                arguments["profile_id"],
                arguments["query"],
                limit=int(arguments.get("limit", 20)),
            )
        if name == "closeout":
            return self.bridge.closeout(
                arguments["profile_id"],
                arguments.get("notes", ""),
                arguments["new_state"],
            )
        if name == "list_stores":
            return self.bridge.list_stores(arguments["profile_id"])
        if name == "propose_store":
            return self.bridge.propose_store(
                arguments["profile_id"],
                arguments["name"],
                arguments["purpose"],
                arguments["schema"],
            )
        if name == "query_records":
            contains = arguments.get("contains")
            return self.bridge.query_records(
                arguments["profile_id"],
                arguments["store_name"],
                contains=contains or None,
                limit=int(arguments.get("limit", 50)),
            )
        if name == "add_record":
            return self.bridge.add_record(
                arguments["profile_id"],
                arguments["store_name"],
                arguments["data"],
            )
        raise ValueError(f"unknown tool {name!r}")


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(
    request_id: Any,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _tool_result(value: Any) -> dict[str, Any]:
    result = {
        "content": [{"type": "text", "text": _json_text(value)}],
        "isError": False,
    }
    result["structuredContent"] = value if isinstance(value, dict) else {"items": value}
    return result


def _tool_error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _origin_allowed(settings: MCPSettings, origin: str | None) -> bool:
    if not origin:
        return True
    if settings.allow_any_origin:
        return True
    if not settings.allowed_origins:
        return _is_local_origin(origin)
    return any(_origin_matches(pattern, origin) for pattern in settings.allowed_origins)


def _cors_headers(settings: MCPSettings, request: Request) -> dict[str, str]:
    origin = request.headers.get("origin")
    if not origin or not _origin_allowed(settings, origin):
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
    }


def _origin_error(settings: MCPSettings, request: Request) -> JSONResponse | None:
    origin = request.headers.get("origin")
    if _origin_allowed(settings, origin):
        return None
    return JSONResponse({"error": "forbidden origin"}, status_code=403)


def _protocol_error(request: Request) -> JSONResponse | None:
    version = request.headers.get("mcp-protocol-version")
    if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
        return JSONResponse(
            {"error": f"unsupported MCP-Protocol-Version {version!r}"},
            status_code=400,
        )
    return None


def _www_authenticate(settings: MCPSettings, request: Request, error: str | None = None) -> str:
    value = f'Bearer resource_metadata="{_metadata_url(settings, request)}"'
    if error:
        value += f', error="{error}"'
    return value


def _unauthorized(
    settings: MCPSettings,
    request: Request,
    error: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": _www_authenticate(settings, request, error)},
    )


def _sign_token(settings: MCPSettings, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = _b64url(raw)
    sig = hmac.new(settings.oauth_signing_key.encode("utf-8"),
                   encoded.encode("ascii"), hashlib.sha256).digest()
    return f"pos.v1.{encoded}.{_b64url(sig)}"


def _validate_oauth_token(
    settings: MCPSettings,
    request: Request,
    token: str,
) -> bool:
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "pos" or parts[1] != "v1":
        return False
    encoded, sig = parts[2], parts[3]
    expected = hmac.new(settings.oauth_signing_key.encode("utf-8"),
                        encoded.encode("ascii"), hashlib.sha256).digest()
    try:
        supplied = _b64url_decode(sig)
    except (ValueError, TypeError):
        return False
    if not hmac.compare_digest(expected, supplied):
        return False
    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
    now = int(time.time())
    if int(payload.get("exp", 0)) <= now:
        return False
    if payload.get("iss") != _issuer_url(settings, request):
        return False
    if payload.get("aud") != _resource_url(settings, request):
        return False
    scope = str(payload.get("scope", ""))
    return SCOPE in scope.split()


def _authenticated(settings: MCPSettings, request: Request) -> JSONResponse | None:
    if not settings.auth_required:
        return None
    header = request.headers.get("authorization") or ""
    if not header.startswith("Bearer "):
        return _unauthorized(settings, request)
    token = header[len("Bearer "):]
    for expected in settings.connector_tokens:
        if hmac.compare_digest(token, expected):
            return None
    if _validate_oauth_token(settings, request, token):
        return None
    return _unauthorized(settings, request, "invalid_token")


def _preflight_headers(settings: MCPSettings, request: Request) -> dict[str, str]:
    headers = _cors_headers(settings, request)
    headers.update({
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": (
            "Authorization, Content-Type, Accept, MCP-Protocol-Version, Mcp-Session-Id"
        ),
        "Access-Control-Max-Age": "600",
    })
    return headers


async def _request_data(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    if "application/json" in content_type:
        return json.loads(raw.decode("utf-8") or "{}")
    parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _safe_profile(arguments: dict[str, Any]) -> str:
    value = arguments.get("profile_id")
    return str(value) if value is not None else "-"


def _handle_rpc(message: dict[str, Any], app: FastAPI) -> dict[str, Any]:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
        return _rpc_result(request_id, {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "profile-os-mcp",
                "title": "Profile OS Remote MCP",
                "version": SERVER_VERSION,
            },
            "instructions": (
                "Use list_profiles to discover available profiles. When asked to act as "
                "a companion, call boot_profile first and use the returned prompts, "
                "compact_state, memory policy, closeout rules, and allowed_tools."
            ),
        })

    if method == "ping":
        return _rpc_result(request_id, {})

    if method == "tools/list":
        return _rpc_result(request_id, {"tools": MCP_TOOLS})

    if method == "tools/call":
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "tools/call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in MCP_TOOL_NAMES:
            return _rpc_error(request_id, -32602, f"unknown tool {name!r}")
        if not isinstance(arguments, dict):
            return _rpc_error(request_id, -32602, "tool arguments must be an object")

        started = time.time()
        profile_id = _safe_profile(arguments)
        try:
            value = app.state.runner.call(name, arguments)
            elapsed_ms = int((time.time() - started) * 1000)
            LOGGER.info(
                "mcp_tool_call name=%s profile_id=%s outcome=ok elapsed_ms=%s",
                name, profile_id, elapsed_ms,
            )
            return _rpc_result(request_id, _tool_result(value))
        except ToolBridgeError as e:
            elapsed_ms = int((time.time() - started) * 1000)
            LOGGER.warning(
                "mcp_tool_call name=%s profile_id=%s outcome=backend_error status=%s elapsed_ms=%s",
                name, profile_id, e.status_code, elapsed_ms,
            )
            return _rpc_result(
                request_id,
                _tool_error(f"backend returned {e.status_code}: {e.detail}"),
            )
        except (KeyError, TypeError, ValueError) as e:
            elapsed_ms = int((time.time() - started) * 1000)
            LOGGER.warning(
                "mcp_tool_call name=%s profile_id=%s outcome=bad_args elapsed_ms=%s",
                name, profile_id, elapsed_ms,
            )
            return _rpc_result(request_id, _tool_error(str(e)))

    return _rpc_error(request_id, -32601, f"method not found: {method}")


def create_mcp_app(
    *,
    bridge: ToolBridge | None = None,
    settings: MCPSettings | None = None,
    oauth_state: OAuthState | None = None,
) -> FastAPI:
    settings = settings or MCPSettings.from_env()
    app = FastAPI(title="Profile OS Remote MCP", version=SERVER_VERSION)
    app.state.settings = settings
    app.state.oauth = oauth_state or OAuthState()
    app.state.runner = MCPToolRunner(bridge or ToolBridge())

    @app.get("/health", name="health")
    async def health():
        return {
            "ok": True,
            "service": "profile-os-mcp",
            "transport": "streamable-http",
            "mcp_endpoint": "/mcp",
            "auth_required": settings.auth_required,
        }

    @app.get("/.well-known/oauth-protected-resource",
             name="oauth_protected_resource_metadata")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def oauth_protected_resource_metadata(request: Request):
        return {
            "resource": _resource_url(settings, request),
            "resource_name": "Profile OS MCP",
            "authorization_servers": [_issuer_url(settings, request)],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [SCOPE],
        }

    @app.get("/.well-known/oauth-authorization-server")
    async def oauth_authorization_server_metadata(request: Request):
        issuer = _issuer_url(settings, request)
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "registration_endpoint": f"{issuer}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
            "resource_indicators_supported": True,
        }

    @app.post("/oauth/register", status_code=201)
    async def oauth_register(request: Request):
        try:
            data = await _request_data(request)
        except (ValueError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
        redirect_uris = data.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return JSONResponse({"error": "redirect_uris required"}, status_code=400)
        if not all(isinstance(uri, str) and _redirect_host_allowed(
            uri, settings.oauth_allowed_redirect_hosts) for uri in redirect_uris):
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
        client = app.state.oauth.register(
            redirect_uris,
            str(data.get("client_name") or "Claude MCP client"),
        )
        return {
            "client_id": client.client_id,
            "client_id_issued_at": client.issued_at,
            "redirect_uris": client.redirect_uris,
            "client_name": client.client_name,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }

    @app.get("/oauth/authorize")
    async def oauth_authorize(request: Request):
        q = request.query_params
        if q.get("response_type") != "code":
            return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
        client_id = q.get("client_id") or ""
        client = app.state.oauth.get_client(client_id)
        if client is None:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        redirect_uri = q.get("redirect_uri") or ""
        if redirect_uri not in client.redirect_uris:
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return JSONResponse({"error": "invalid_request",
                                 "error_description": "PKCE S256 is required"},
                                status_code=400)
        resource = q.get("resource") or _resource_url(settings, request)
        if resource != _resource_url(settings, request):
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        code = app.state.oauth.create_code(
            client_id, redirect_uri, q["code_challenge"], resource)
        params = {"code": code}
        if q.get("state") is not None:
            params["state"] = q["state"]
        separator = "&" if urllib.parse.urlparse(redirect_uri).query else "?"
        location = redirect_uri + separator + urllib.parse.urlencode(params)
        return RedirectResponse(location)

    @app.post("/oauth/token")
    async def oauth_token(request: Request):
        try:
            data = await _request_data(request)
        except (ValueError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        if data.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        code = app.state.oauth.consume_code(str(data.get("code") or ""))
        if code is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if data.get("client_id") != code.client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        if data.get("redirect_uri") != code.redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        verifier = str(data.get("code_verifier") or "")
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        if not verifier or not hmac.compare_digest(challenge, code.code_challenge):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        now = int(time.time())
        token = _sign_token(settings, {
            "iss": _issuer_url(settings, request),
            "aud": code.resource,
            "sub": f"client:{code.client_id}",
            "client_id": code.client_id,
            "scope": SCOPE,
            "iat": now,
            "exp": now + settings.oauth_token_ttl_seconds,
        })
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": settings.oauth_token_ttl_seconds,
            "scope": SCOPE,
        }

    @app.options("/mcp")
    async def mcp_options(request: Request):
        origin_error = _origin_error(settings, request)
        if origin_error:
            return origin_error
        return Response(status_code=204, headers=_preflight_headers(settings, request))

    @app.api_route("/mcp", methods=["GET", "POST"], name="mcp_endpoint")
    async def mcp_endpoint(request: Request):
        origin_error = _origin_error(settings, request)
        if origin_error:
            return origin_error
        protocol_error = _protocol_error(request)
        if protocol_error:
            return protocol_error
        auth_error = _authenticated(settings, request)
        if auth_error:
            return auth_error

        headers = {
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            **_cors_headers(settings, request),
        }

        if request.method == "GET":
            accept = request.headers.get("accept", "")
            if "text/event-stream" not in accept and "*/*" not in accept:
                return Response(status_code=405, headers=headers)
            return Response(
                content=": profile-os-mcp connected\n\n",
                media_type="text/event-stream",
                headers=headers,
            )

        try:
            message = await request.json()
        except (ValueError, json.JSONDecodeError):
            return JSONResponse(
                _rpc_error(None, -32700, "parse error"),
                status_code=400,
                headers=headers,
            )

        if not isinstance(message, dict):
            return JSONResponse(
                _rpc_error(None, -32600, "invalid request"),
                status_code=400,
                headers=headers,
            )

        # JSON-RPC notifications and responses do not receive a JSON body over
        # Streamable HTTP. The initialized notification is the common one.
        if "id" not in message:
            return Response(status_code=202, headers=headers)
        if "method" not in message:
            return Response(status_code=202, headers=headers)

        response = _handle_rpc(message, app)
        return JSONResponse(response, headers=headers)

    return app


app = create_mcp_app()
