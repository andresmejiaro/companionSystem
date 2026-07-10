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

import html as _html
from typing import Awaitable, Callable

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .bridge import ToolBridge, ToolBridgeError
from .tool_schemas import (
    APPROVAL,
    BOOT,
    CLOSEOUT,
    DELETED_FILE,
    DELETED_MEMORY,
    DYNAMIC_RECORD,
    DYNAMIC_STORE,
    FILE_CONTENT,
    FILE_META,
    IDENTITY,
    MEMORY_EVENT,
    MEMORY_KINDS,
    MESSAGE,
    PROFILE,
    START_SESSION,
    mcp_items,
)

AdminVerifyFn = Callable[[str, str], Awaitable[bool]]

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


async def default_admin_verify(secret: str, totp_code: str) -> bool:
    """Calls the backend's login-check route. Injected so tests can stub it
    without a real backend — see create_mcp_app's admin_verify param."""
    base_url = os.environ.get("PROFILE_OS_BRIDGE_BASE_URL", "http://127.0.0.1:8000")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(f"{base_url}/admin/verify-totp",
                                  json={"secret": secret, "totp_code": totp_code})
    except httpx.HTTPError:
        return False
    return r.status_code == 200


def _consent_page(params: dict[str, str], client_name: str,
                  error: str | None = None) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{_html.escape(k)}" value="{_html.escape(v)}">'
        for k, v in params.items() if v is not None
    )
    error_html = (f'<p style="color:#c00;font-weight:600">{_html.escape(error)}</p>'
                 if error else "")
    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize {_html.escape(client_name)}</title></head>
<body style="font-family:system-ui,sans-serif;max-width:420px;margin:64px auto;padding:0 16px">
<h2>Authorize connector</h2>
<p><strong>{_html.escape(client_name)}</strong> wants access to Profile OS.</p>
{error_html}
<form method="POST">
{hidden}
<label>Admin secret<br>
<input type="password" name="admin_secret" autocomplete="off" required
 style="width:100%;padding:8px;margin:4px 0 16px"></label>
<label>Authenticator code<br>
<input type="text" name="totp_code" inputmode="numeric" pattern="[0-9]*"
 autocomplete="off" required style="width:100%;padding:8px;margin:4px 0 16px"></label>
<button type="submit" style="padding:10px 20px">Approve</button>
</form>
</body></html>"""


def _approval_page(approval: dict, error: str | None = None) -> str:
    """TOTP-only convenience page for a companion's proposed prompt edit —
    deliberately lighter than the OAuth login (no admin secret): see
    ACCESS_CONTROL.md 'TOTP-only approval links'."""
    payload = approval.get("payload") or {}
    fields = "".join(
        f'<h4>{_html.escape(k)}</h4><pre style="white-space:pre-wrap;background:#f4f4f4;'
        f'padding:12px;border-radius:6px">{_html.escape(str(v))}</pre>'
        for k, v in payload.items() if v is not None
    )
    error_html = (f'<p style="color:#c00;font-weight:600">{_html.escape(error)}</p>'
                 if error else "")
    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approve edit</title></head>
<body style="font-family:system-ui,sans-serif;max-width:480px;margin:48px auto;padding:0 16px">
<h2>Proposed {_html.escape(approval.get('kind', 'edit'))}</h2>
<p>Profile: <strong>{_html.escape(str(approval.get('profile_id')))}</strong></p>
{fields}
{error_html}
<form method="POST">
<label>Authenticator code<br>
<input type="text" name="totp_code" inputmode="numeric" pattern="[0-9]*"
 autocomplete="off" autofocus required style="width:100%;padding:10px;margin:4px 0 16px;font-size:1.2em">
</label>
<button type="submit" name="decision" value="approve"
 style="padding:10px 20px;margin-right:8px">Approve</button>
<button type="submit" name="decision" value="reject"
 style="padding:10px 20px">Reject</button>
</form>
</body></html>"""


def _create_profile_page(values: dict[str, str] | None = None,
                         error: str | None = None, created: dict | None = None) -> str:
    """TOTP-only page for creating (or migrating) a companion from mobile,
    without the admin secret or SSH — see ACCESS_CONTROL.md 'TOTP-only
    profile creation'."""
    v = values or {}
    if created:
        return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Profile created</title></head>
<body style="font-family:system-ui,sans-serif;max-width:480px;margin:48px auto;padding:0 16px">
<h2>Created</h2>
<p><strong>{_html.escape(created.get('id', ''))}</strong> ({_html.escape(created.get('display_name', ''))}) is ready.</p>
<p><a href="/create-profile">Create another</a></p>
</body></html>"""
    error_html = (f'<p style="color:#c00;font-weight:600">{_html.escape(error)}</p>'
                 if error else "")

    def _f(name: str) -> str:
        return _html.escape(v.get(name) or "")

    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Create companion</title></head>
<body style="font-family:system-ui,sans-serif;max-width:480px;margin:48px auto;padding:0 16px">
<h2>Create a companion</h2>
{error_html}
<form method="POST">
<label>Profile id (lowercase, - or _, no spaces)<br>
<input type="text" name="id" value="{_f('id')}" pattern="[a-z0-9_-]{{1,64}}" required
 style="width:100%;padding:8px;margin:4px 0 16px"></label>
<label>Display name<br>
<input type="text" name="display_name" value="{_f('display_name')}" required
 style="width:100%;padding:8px;margin:4px 0 16px"></label>
<label>Base prompt (optional — can self-define later)<br>
<textarea name="base_prompt" rows="4"
 style="width:100%;padding:8px;margin:4px 0 16px">{_f('base_prompt')}</textarea></label>
<label>Role prompt (optional)<br>
<textarea name="role_prompt" rows="4"
 style="width:100%;padding:8px;margin:4px 0 16px">{_f('role_prompt')}</textarea></label>
<label>Authenticator code<br>
<input type="text" name="totp_code" inputmode="numeric" pattern="[0-9]*"
 autocomplete="off" required style="width:100%;padding:10px;margin:4px 0 16px;font-size:1.2em">
</label>
<button type="submit" style="padding:10px 20px">Create</button>
</form>
</body></html>"""


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
        "outputSchema": MCP_OUTPUT_SCHEMAS[name],
    }


_PROFILE_ID = {
    "type": "string",
    "description": "Profile id to operate on, such as a value returned by list_profiles.",
}

_MEMORY_KINDS = MEMORY_KINDS

MCP_OUTPUT_SCHEMAS = {
    "whoami": IDENTITY,
    "list_profiles": mcp_items(PROFILE),
    "boot_profile": BOOT,
    "start_session": START_SESSION,
    "propose_prompt_edit": APPROVAL,
    "update_own_description": PROFILE,
    "remember": MEMORY_EVENT,
    "search_memories": mcp_items(MEMORY_EVENT),
    "update_memory": MEMORY_EVENT,
    "forget": DELETED_MEMORY,
    "send_message": MESSAGE,
    "read_inbox": mcp_items(MESSAGE),
    "mark_message_read": MESSAGE,
    "write_file": FILE_META,
    "list_files": mcp_items(FILE_META),
    "read_file": FILE_CONTENT,
    "delete_file": DELETED_FILE,
    "closeout": CLOSEOUT,
    "list_stores": mcp_items(DYNAMIC_STORE),
    "propose_store": DYNAMIC_STORE,
    "query_records": mcp_items(DYNAMIC_RECORD),
    "add_record": DYNAMIC_RECORD,
}

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
        "start_session",
        "Start Session",
        "Call this on your first response in a conversation instead of boot_profile:"
        " returns whoami identity, prompts, compact_state, the last 2 closeouts"
        " (not just the current state), your full memory history, and the current"
        " server date/time (server_time) in one call.",
        {"profile_id": _PROFILE_ID},
        ["profile_id"],
    ),
    _tool(
        "propose_prompt_edit",
        "Propose Prompt Edit",
        "Propose a change to your own base_prompt and/or role_prompt. Held pending"
        " until the human approves it with a live TOTP code from their authenticator"
        " app — you cannot approve your own edits, and there is no way around that.",
        {
            "profile_id": _PROFILE_ID,
            "base_prompt": {"type": "string"},
            "role_prompt": {"type": "string"},
        },
        ["profile_id"],
    ),
    _tool(
        "update_own_description",
        "Update Own Description",
        "Update your own one-line 'what do I do' description — self-service, no"
        " approval. Other companions see it via list_profiles to know who to ask"
        " about what, instead of a human hardcoding names into prompts.",
        {"profile_id": _PROFILE_ID, "description": {"type": "string"}},
        ["profile_id", "description"],
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
        "update_memory",
        "Update Memory",
        "Revise one of your own memory events (kind/content/tags). Self-service —"
        " no approval needed, same trust level as remembering one.",
        {
            "profile_id": _PROFILE_ID,
            "event_id": {"type": "string"},
            "kind": {"type": "string", "enum": _MEMORY_KINDS},
            "content": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        ["profile_id", "event_id"],
    ),
    _tool(
        "forget",
        "Forget",
        "Permanently erase one of your own memory events. Self-service — no"
        " approval needed.",
        {"profile_id": _PROFILE_ID, "event_id": {"type": "string"}},
        ["profile_id", "event_id"],
    ),
    _tool(
        "send_message",
        "Send Message",
        "Send a message to another profile's inbox — for handing something off"
        " to another companion without a human copy-pasting between conversations.",
        {
            "profile_id": _PROFILE_ID,
            "to_profile_id": {"type": "string", "description": "recipient profile id"},
            "content": {"type": "string"},
        },
        ["profile_id", "to_profile_id", "content"],
    ),
    _tool(
        "read_inbox",
        "Read Inbox",
        "Read messages sent to you by other profiles.",
        {
            "profile_id": _PROFILE_ID,
            "unread_only": {"type": "boolean", "default": True},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
        ["profile_id"],
    ),
    _tool(
        "mark_message_read",
        "Mark Message Read",
        "Mark one of your inbox messages as read.",
        {"profile_id": _PROFILE_ID, "message_id": {"type": "string"}},
        ["profile_id", "message_id"],
    ),
    _tool(
        "write_file",
        "Write File",
        "Write (or overwrite) a plain file in your own scratch file store — for"
        " scripts, notes, anything that doesn't belong as a structured record."
        " Self-service, never in git, never a database blob. Max 256KB.",
        {
            "profile_id": _PROFILE_ID,
            "filename": {"type": "string",
                        "description": "e.g. 'notes.md' or 'script.py'; no path separators"},
            "content": {"type": "string"},
        },
        ["profile_id", "filename", "content"],
    ),
    _tool(
        "list_files",
        "List Files",
        "List files in your scratch file store.",
        {"profile_id": _PROFILE_ID},
        ["profile_id"],
    ),
    _tool(
        "read_file",
        "Read File",
        "Read a file from your scratch file store.",
        {"profile_id": _PROFILE_ID, "filename": {"type": "string"}},
        ["profile_id", "filename"],
    ),
    _tool(
        "delete_file",
        "Delete File",
        "Delete a file from your scratch file store.",
        {"profile_id": _PROFILE_ID, "filename": {"type": "string"}},
        ["profile_id", "filename"],
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
    """Registered clients persist to disk so a redeploy (docker compose up
    --build recreates the container, wiping in-process memory) doesn't
    invalidate every connector that already completed dynamic client
    registration — Claude/ChatGPT don't re-register on their own, so a lost
    client_id shows up to the user as a mysterious 'invalid_client' 400.
    Codes stay in-memory only; their 5-minute TTL makes restart-loss a
    non-issue (worst case: redo one authorize round-trip)."""

    def __init__(self, state_file: str | None = None):
        self._lock = threading.Lock()
        self._clients: dict[str, OAuthClient] = {}
        self._codes: dict[str, OAuthCode] = {}
        self._state_file = state_file
        self._load()

    def _load(self) -> None:
        if not self._state_file or not os.path.isfile(self._state_file):
            return
        try:
            with open(self._state_file) as f:
                raw = json.load(f)
            self._clients = {cid: OAuthClient(**data) for cid, data in raw.items()}
        except (OSError, ValueError, TypeError):
            LOGGER.warning("failed to load OAuth client state from %s", self._state_file)

    def _save(self) -> None:
        if not self._state_file:
            return
        try:
            os.makedirs(os.path.dirname(self._state_file) or ".", exist_ok=True)
            tmp = f"{self._state_file}.tmp"
            with open(tmp, "w") as f:
                json.dump({cid: c.__dict__ for cid, c in self._clients.items()}, f)
            os.replace(tmp, self._state_file)
        except OSError:
            LOGGER.warning("failed to persist OAuth client state to %s", self._state_file)

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
            self._save()
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
        if name == "start_session":
            return self.bridge.start_session(arguments["profile_id"])
        if name == "propose_prompt_edit":
            return self.bridge.propose_prompt_edit(
                arguments["profile_id"],
                base_prompt=arguments.get("base_prompt"),
                role_prompt=arguments.get("role_prompt"),
            )
        if name == "update_own_description":
            return self.bridge.update_own_description(
                arguments["profile_id"], arguments["description"])
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
        if name == "update_memory":
            return self.bridge.update_memory(
                arguments["profile_id"],
                arguments["event_id"],
                kind=arguments.get("kind"),
                content=arguments.get("content"),
                tags=arguments.get("tags"),
            )
        if name == "forget":
            return self.bridge.forget(arguments["profile_id"], arguments["event_id"])
        if name == "send_message":
            return self.bridge.send_message(
                arguments["profile_id"], arguments["to_profile_id"], arguments["content"])
        if name == "read_inbox":
            return self.bridge.read_inbox(
                arguments["profile_id"],
                unread_only=arguments.get("unread_only", True),
                limit=int(arguments.get("limit", 50)),
            )
        if name == "mark_message_read":
            return self.bridge.mark_message_read(
                arguments["profile_id"], arguments["message_id"])
        if name == "write_file":
            return self.bridge.write_file(
                arguments["profile_id"], arguments["filename"], arguments["content"])
        if name == "list_files":
            return self.bridge.list_files(arguments["profile_id"])
        if name == "read_file":
            return self.bridge.read_file(arguments["profile_id"], arguments["filename"])
        if name == "delete_file":
            return self.bridge.delete_file(arguments["profile_id"], arguments["filename"])
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
            if name in {"propose_prompt_edit", "propose_store"} and isinstance(value, dict):
                settings: MCPSettings = app.state.settings
                approval_id = (value.get("id") if name == "propose_prompt_edit"
                               else value.get("approval_id"))
                if settings.public_base_url and approval_id:
                    value = {**value, "approval_link":
                            f"{_canonical_base(settings.public_base_url)}/approvals/{approval_id}"}
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
    admin_verify: AdminVerifyFn | None = None,
) -> FastAPI:
    settings = settings or MCPSettings.from_env()
    app = FastAPI(title="Profile OS Remote MCP", version=SERVER_VERSION)
    app.state.settings = settings
    app.state.oauth = oauth_state or OAuthState(
        state_file=os.environ.get("MCP_OAUTH_STATE_FILE"))
    app.state.runner = MCPToolRunner(bridge or ToolBridge())
    app.state.admin_verify = admin_verify or default_admin_verify
    _authorize_hits: dict[str, list[float]] = {}
    _approval_hits: dict[str, list[float]] = {}
    _create_profile_hits: dict[str, list[float]] = {}

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

    def _validate_authorize_params(params, request: Request):
        """Returns (error_response, None) or (None, {client, redirect_uri,
        code_challenge, resource, state}). Shared by GET (render form) and
        POST (re-validate before issuing a code) so a tampered hidden field
        can't bypass checks the GET already did."""
        if params.get("response_type") != "code":
            return JSONResponse({"error": "unsupported_response_type"}, status_code=400), None
        client_id = params.get("client_id") or ""
        client = app.state.oauth.get_client(client_id)
        if client is None:
            return JSONResponse({"error": "invalid_client"}, status_code=400), None
        redirect_uri = params.get("redirect_uri") or ""
        if redirect_uri not in client.redirect_uris:
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400), None
        if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
            return JSONResponse({"error": "invalid_request",
                                 "error_description": "PKCE S256 is required"},
                                status_code=400), None
        resource = params.get("resource") or _resource_url(settings, request)
        if resource != _resource_url(settings, request):
            return JSONResponse({"error": "invalid_target"}, status_code=400), None
        return None, {
            "client": client, "client_id": client_id, "redirect_uri": redirect_uri,
            "code_challenge": params["code_challenge"], "resource": resource,
            "state": params.get("state"),
        }

    _AUTHORIZE_FIELDS = ("response_type", "client_id", "redirect_uri",
                        "code_challenge", "code_challenge_method", "resource", "state")

    @app.get("/oauth/authorize")
    async def oauth_authorize(request: Request):
        """Renders a login form instead of auto-issuing a code: dynamic
        client registration is open by design (any MCP client can call
        /oauth/register), so without a human check here anyone who finds
        this URL could mint themselves a valid access token. See
        ACCESS_CONTROL.md 'OAuth authorize consent screen'."""
        error, validated = _validate_authorize_params(request.query_params, request)
        if error is not None:
            return error
        hidden = {k: request.query_params.get(k) for k in _AUTHORIZE_FIELDS}
        return HTMLResponse(_consent_page(hidden, validated["client"].client_name))

    @app.post("/oauth/authorize")
    async def oauth_authorize_decide(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        hits = [t for t in _authorize_hits.get(client_ip, []) if t > now - 60]
        if len(hits) >= 5:
            return HTMLResponse("Too many attempts; try again in a minute.",
                                status_code=429)
        hits.append(now)
        _authorize_hits[client_ip] = hits

        form = await request.form()
        error, validated = _validate_authorize_params(form, request)
        if error is not None:
            return error
        hidden = {k: form.get(k) for k in _AUTHORIZE_FIELDS}
        secret = str(form.get("admin_secret") or "")
        totp_code = str(form.get("totp_code") or "")
        if not await app.state.admin_verify(secret, totp_code):
            return HTMLResponse(
                _consent_page(hidden, validated["client"].client_name,
                             error="Invalid secret or code."),
                status_code=401)
        code = app.state.oauth.create_code(
            validated["client_id"], validated["redirect_uri"],
            validated["code_challenge"], validated["resource"])
        params = {"code": code}
        if validated["state"] is not None:
            params["state"] = validated["state"]
        separator = "&" if urllib.parse.urlparse(validated["redirect_uri"]).query else "?"
        location = validated["redirect_uri"] + separator + urllib.parse.urlencode(params)
        return RedirectResponse(location, status_code=303)

    @app.get("/approvals/{approval_id}")
    async def approval_page(approval_id: str):
        """Public, TOTP-only link for a companion's proposed prompt edit —
        see ACCESS_CONTROL.md 'TOTP-only approval links'. Deliberately
        lighter than the OAuth login: no admin secret, just a live code,
        since this is meant to be usable from a phone with only the
        authenticator app open."""
        try:
            approval = await run_in_threadpool(
                app.state.runner.bridge.get_approval, approval_id)
        except ToolBridgeError as e:
            return HTMLResponse(f"Approval not found: {e.detail}", status_code=e.status_code)
        if approval.get("status") != "pending":
            return HTMLResponse(f"Already {approval.get('status')}. Nothing to do.")
        return HTMLResponse(_approval_page(approval))

    @app.post("/approvals/{approval_id}")
    async def approval_decide(approval_id: str, request: Request):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        hits = [t for t in _approval_hits.get(client_ip, []) if t > now - 60]
        if len(hits) >= 5:
            return HTMLResponse("Too many attempts; try again in a minute.",
                                status_code=429)
        hits.append(now)
        _approval_hits[client_ip] = hits

        form = await request.form()
        totp_code = str(form.get("totp_code") or "")
        approve = str(form.get("decision") or "") == "approve"
        try:
            decided = await run_in_threadpool(
                app.state.runner.bridge.decide_approval, approval_id, approve, totp_code)
        except ToolBridgeError as e:
            try:
                approval = await run_in_threadpool(
                    app.state.runner.bridge.get_approval, approval_id)
            except ToolBridgeError:
                return HTMLResponse(f"Error: {e.detail}", status_code=e.status_code)
            return HTMLResponse(_approval_page(approval, error=e.detail),
                                status_code=e.status_code)
        return HTMLResponse(f"<p>Done — {_html.escape(decided['status'])}.</p>")

    @app.get("/create-profile")
    async def create_profile_page():
        """Public, TOTP-only page for creating/migrating a companion from
        mobile — no admin secret, no SSH. See ACCESS_CONTROL.md 'TOTP-only
        profile creation'."""
        return HTMLResponse(_create_profile_page())

    @app.post("/create-profile")
    async def create_profile_submit(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        hits = [t for t in _create_profile_hits.get(client_ip, []) if t > now - 60]
        if len(hits) >= 5:
            return HTMLResponse("Too many attempts; try again in a minute.",
                                status_code=429)
        hits.append(now)
        _create_profile_hits[client_ip] = hits

        form = await request.form()
        values = {k: str(form.get(k) or "") for k in
                 ("id", "display_name", "base_prompt", "role_prompt")}
        totp_code = str(form.get("totp_code") or "")
        try:
            created = await run_in_threadpool(
                app.state.runner.bridge.create_profile_totp,
                values["id"], values["display_name"],
                values["base_prompt"], values["role_prompt"], totp_code)
        except ToolBridgeError as e:
            return HTMLResponse(_create_profile_page(values, error=e.detail),
                                status_code=e.status_code)
        return HTMLResponse(_create_profile_page(created=created))

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
