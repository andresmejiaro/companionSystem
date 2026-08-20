"""Remote MCP server for Profile OS over Streamable HTTP.

This process is an adapter, not a second backend. It validates remote MCP
client credentials, exposes Profile OS operations as MCP tools, and then calls
the existing HTTP backend through ToolBridge using a separate backend bearer
from env. Incoming Claude/client tokens are never forwarded to Profile OS.
"""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
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
from .request_limits import (
    RequestBodyTooLarge,
    configured_max_request_bytes,
    read_request_body,
    replay_request_body,
)
from .tool_schemas import (
    APPROVAL,
    MCP_CLOSEOUT,
    CONTEXT_RESULT,
    DELETED_FILE,
    DELETED_MEMORY,
    DELETED_RECORD,
    DYNAMIC_RECORD,
    DYNAMIC_SCHEMA,
    DYNAMIC_STORE,
    FILE_CONTENT,
    FILE_META,
    EXAM_ATTEMPT,
    EXAM_REVIEW,
    IRONSWORN_RESOURCE,
    IRONSWORN_SHEET,
    MEMORY_EVENT,
    MEMORY_KINDS,
    MESSAGE,
    PROFILE,
    SHARED_DEFS,
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


def _rate_limited(bucket: dict[str, list[float]], key: str,
                  limit: int = 5, window: float = 60) -> bool:
    """Fixed-window-ish in-memory limiter for public, TOTP-gated forms."""
    now = time.time()
    for old_key, old_hits in list(bucket.items()):
        if not any(seen > now - window for seen in old_hits):
            del bucket[old_key]
    hits = [seen for seen in bucket.get(key, []) if seen > now - window]
    hits.append(now)
    bucket[key] = hits
    return len(hits) > limit


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
    if approval.get("kind") == "prompt_edit":
        current = approval.get("current_sections") or {}
        def prompt_field(name: str) -> str:
            value = payload.get(name)
            before = current.get(name, "")
            if value is None:
                body = '<p style="color:#555"><em>No change proposed — the current value remains.</em></p>'
            else:
                body = (f'<p><strong>Current</strong></p><pre style="white-space:pre-wrap;background:#f4f4f4;padding:12px;border-radius:6px">{_html.escape(str(before))}</pre>'
                        f'<p><strong>Proposed{(" (empty)" if value == "" else "")}</strong></p><pre style="white-space:pre-wrap;background:#f4f4f4;padding:12px;border-radius:6px">{_html.escape(str(value))}</pre>'
                        f'<details><summary>Readable diff</summary><pre style="white-space:pre-wrap">{_html.escape("- " + str(before) + "\\n+ " + str(value))}</pre></details>')
            return f'<h4>{_html.escape(name)}</h4>{body}'
        fields = "".join(prompt_field(name) for name in (
            "who_you_are", "signature", "lane", "voice", "what_you_do", "how_you_keep_context"))
    else:
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
<label>Description (optional, up to 200 characters)<br>
<textarea name="description" rows="3" maxlength="200"
 style="width:100%;padding:8px;margin:4px 0 16px">{_f('description')}</textarea></label>
<label>Signature (optional, up to 5 characters)<br>
<input type="text" name="signature" value="{_f('signature')}" maxlength="5"
 style="width:100%;padding:8px;margin:4px 0 16px"></label>
<label>Who you are (optional — can self-define later)<br>
<textarea name="who_you_are" rows="4"
 style="width:100%;padding:8px;margin:4px 0 16px">{_f('who_you_are')}</textarea></label>
<label>What you do (optional)<br>
<textarea name="what_you_do" rows="4"
 style="width:100%;padding:8px;margin:4px 0 16px">{_f('what_you_do')}</textarea></label>
<label>Authenticator code<br>
<input type="text" name="totp_code" inputmode="numeric" pattern="[0-9]*"
 autocomplete="off" required style="width:100%;padding:10px;margin:4px 0 16px;font-size:1.2em">
</label>
<button type="submit" style="padding:10px 20px">Create</button>
</form>
</body></html>"""


def _session_inspector_page(profiles: list[dict], *, selected_id: str = "",
                            mode: str = "human", result: dict | None = None,
                            error: str | None = None) -> str:
    """Read-only view of the exact summon_companion result, gated by TOTP."""
    options = "".join(
        f'<option value="{_html.escape(str(p.get("id", "")))}"'
        f'{" selected" if p.get("id") == selected_id else ""}>'
        f'{_html.escape(str(p.get("id", "")))}</option>'
        for p in profiles
    )
    error_html = f'<p class="error">{_html.escape(error)}</p>' if error else ""
    checked_human = "checked" if mode != "raw" else ""
    checked_raw = "checked" if mode == "raw" else ""
    output = ""
    if result is not None:
        if mode == "raw":
            output = ("<section><h2>Delivered payload</h2><p>This is the pretty-printed JSON "
                      "returned by <code>summon_companion</code>.</p><pre>" +
                      _html.escape(_json_text(result)) + "</pre></section>")
        else:
            def block(title: str, source: str, value: Any) -> str:
                text = _html.escape(_json_text(value) if isinstance(value, (dict, list)) else str(value or ""))
                return (f'<section><h2>{_html.escape(title)}</h2><p class="source">'
                        f'{_html.escape(source)}</p><pre>{text}</pre></section>')
            profile = result.get("profile") or {}
            profile_context = {
                key: profile[key] for key in ("display_name", "allowed_tools", "closeout_rules")
                if profile.get(key) not in (None, "", [])
            }
            current_state = result.get("compact_state") or ""
            memory_context = [
                {key: item.get(key) for key in ("kind", "content", "created_at") if item.get(key) is not None}
                for item in result.get("memories", [])
            ]
            output = "<section><h2>Hydration packet</h2><p>This is the context delivered to the agent. Lookup IDs, tags, full history, and closeout archives are not hydrated.</p></section>"
            output += block("Profile context", "Profile registry fields that affect how this companion operates.", profile_context)
            output += block("System contracts", "Shared runtime rules injected by summon_companion; they do not replace this companion's identity prompt.", result.get("system_contracts"))
            output += block("Who you are", "Canonical prompt section.", result.get("who_you_are"))
            output += block("Signature", "Reserved prompt section.", result.get("signature"))
            output += block("Lane", "Reserved prompt section.", result.get("lane"))
            output += block("Voice", "Reserved prompt section.", result.get("voice"))
            output += block("What you do", "Canonical prompt section.", result.get("what_you_do"))
            output += block("How you keep context", "Reserved prompt section.", result.get("how_you_keep_context"))
            output += block("Current handoff", "Current compact state, written at closeout. This is the latest session handoff.", current_state)
            output += block("Global identity (whoami)", "Canonical external identity file, when the bridge has identity:read.", result.get("identity"))
            output += block("Memories", "Mutable context, newest first. Tags and database IDs are intentionally hidden here; use the raw payload for lookup fields.", memory_context)
            output += block("Data sources", "Profile stores and shared projects this companion may use in this session.", result.get("data_sources"))
            output += block("Companion directory", "The current companion cast; it does not include private context.", result.get("companion_directory"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Companion session inspector</title><style>
body {{ font:16px/1.45 system-ui,sans-serif; max-width:1000px; margin:32px auto; padding:0 18px; color:#18212b; background:#fafbfc; }}
h1 {{ margin-bottom:.15em; }} .hint,.source {{ color:#52616f; }} .source {{ margin-top:-.6em; font-size:.92em; }}
form,section {{ background:#fff; border:1px solid #d9e0e6; border-radius:10px; padding:18px; margin:18px 0; }}
label {{ display:block; font-weight:600; margin:.6em 0; }} select,input,button {{ font:inherit; padding:.55em; }} select,input {{ min-width:18rem; }}
fieldset {{ border:0; padding:0; margin:1em 0; }} fieldset label {{ display:inline; margin-right:1.2em; font-weight:400; }}
button {{ background:#1769aa; color:white; border:0; border-radius:6px; cursor:pointer; }} .error {{ color:#a11; font-weight:600; }}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; background:#f3f6f8; padding:14px; border-radius:7px; max-height:34rem; overflow:auto; }} code {{ background:#eef2f5; padding:.1em .25em; }}
</style></head><body><main><h1>Companion session inspector</h1><p class="hint">Read-only. Enter a live authenticator code to view exactly what a companion receives from <code>summon_companion</code>.</p>
{error_html}<form method="post"><label>Companion<br><select name="profile_id" required><option value="">Choose a companion…</option>{options}</select></label>
<label>Authenticator code<br><input name="totp_code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6,8}}" required></label>
<fieldset><legend>Display</legend><label><input type="radio" name="mode" value="human" {checked_human}> Human-readable, grouped by source</label><label><input type="radio" name="mode" value="raw" {checked_raw}> Raw delivered JSON</label></fieldset><button type="submit">View session</button></form>{output}</main></body></html>"""


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
    """Render tool text as readable Unicode rather than ASCII escape sequences."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _tool(
    name: str,
    title: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    annotations: dict[str, bool] | None = None,
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
            "$defs": SHARED_DEFS,
        },
        "outputSchema": {**MCP_OUTPUT_SCHEMAS[name], "$defs": SHARED_DEFS},
        "annotations": annotations or TOOL_ANNOTATIONS[name],
    }


_PROFILE_ID = {
    "type": "string",
    "description": "Profile id to operate on, such as a value returned by discover_companions.",
}

_MEMORY_KINDS = MEMORY_KINDS

MCP_OUTPUT_SCHEMAS = {
    # Kept as an internal summon_companion fallback, not a callable MCP tool.
    # "resolve_companion": PROFILE_RESOLUTION,
    "discover_companions": mcp_items(PROFILE),
    "summon_companion": START_SESSION,
    "propose_prompt_edit": APPROVAL,
    # Legacy backend/admin operation; no companion-callable MCP path.
    # "update_own_description": PROFILE,
    "remember": MEMORY_EVENT,
    "search_memories": mcp_items(MEMORY_EVENT),
    "search_context": mcp_items(CONTEXT_RESULT),
    "update_memory": MEMORY_EVENT,
    "forget": DELETED_MEMORY,
    "send_message": MESSAGE,
    "read_inbox": mcp_items(MESSAGE),
    "set_messages_read_status": mcp_items(MESSAGE),
    "write_file": FILE_META,
    "list_files": mcp_items(FILE_META),
    "read_file": FILE_CONTENT,
    "get_ironsworn_resource": IRONSWORN_RESOURCE,
    "update_ironsworn_sheet": IRONSWORN_SHEET,
    "delete_file": DELETED_FILE,
    "closeout": MCP_CLOSEOUT,
    "list_stores": mcp_items(DYNAMIC_STORE),
    "propose_store": DYNAMIC_STORE,
    "query_records": mcp_items(DYNAMIC_RECORD),
    "exam_attempt": EXAM_ATTEMPT,
    "exam_review": EXAM_REVIEW,
    "get_record": DYNAMIC_RECORD,
    "update_record": DYNAMIC_RECORD,
    "delete_record": DELETED_RECORD,
    "add_records": mcp_items(DYNAMIC_RECORD),
}


_READ_ONLY_TOOLS = {
    "resolve_companion", "discover_companions", "summon_companion", "search_memories", "search_context",
    "read_inbox", "list_files", "read_file", "get_ironsworn_resource",
    "list_stores", "query_records", "get_record",
}
_OPEN_WORLD_TOOLS = {"send_message"}
_DESTRUCTIVE_TOOLS = {"forget", "delete_file", "delete_record"}
_IDEMPOTENT_TOOLS = {"set_messages_read_status"}
TOOL_ANNOTATIONS = {
    name: {
        "readOnlyHint": name in _READ_ONLY_TOOLS,
        "destructiveHint": name in _DESTRUCTIVE_TOOLS,
        "idempotentHint": name in _IDEMPOTENT_TOOLS,
        "openWorldHint": name in _OPEN_WORLD_TOOLS,
    }
    for name in MCP_OUTPUT_SCHEMAS
}

MCP_TOOLS = [
    # resolve_companion intentionally omitted. summon_companion keeps its private
    # resolver fallback, so routing behavior remains unchanged without a raw
    # profile-resolution read surface.
    _tool(
        "discover_companions",
        "Discover Companions",
        "Browse every available companion with canonical id, display name, signature, and"
        " lane. Do not call this before summon_companion merely because the"
        " user supplied a name: summon_companion tries normalized exact canonical ids"
        " first and falls back to server resolution. Use discovery for browsing or"
        " after a not_found result.",
        {},
        [],
    ),
    _tool(
        "summon_companion",
        "Summon Companion",
        "Call this on your first response in a conversation."
        " Pass the phrase the user supplied. A normalized exact canonical id is"
        " decisive and is tried directly; only a 404 triggers server-side resolution."
        " The returned selection identifies the active companion. This tool"
        " returns owner identity, prompts, compact_state, a bounded semantic"
        " memory slice, and the global companion contract for conversational profiles;"
        " the memory slice has no IDs, tags, or full history. It also returns up to four recent"
        " texture/exchange examples for continuity, and the current server"
        " date/time (server_time, including Madrid time) in one call.",
        {"profile_id": _PROFILE_ID,
         "mode": {
             "type": "string",
             "enum": ["conversation", "forum"],
             "default": "conversation",
             "description": "conversation returns the owner identity; forum omits it and adds bounded thread continuity for autonomous forum wakes.",
         }},
        ["profile_id"],
    ),
    _tool(
        "propose_prompt_edit",
        "Propose Prompt Edit",
        "Propose changes to your own canonical prompt sections and companion-specific"
        " closeout rules. Held pending"
        " until the human approves it with a live TOTP code from their authenticator"
        " app. If the result includes `approval_link`, include that link in the"
        " user-facing response.",
        {
            "profile_id": _PROFILE_ID,
            "who_you_are": {"type": "string"},
            "signature": {"type": "string", "description": "Up to five emoji grapheme clusters."},
            "lane": {"type": "string", "maxLength": 200, "description": "One-line public directory summary."},
            "voice": {"type": "string"},
            "what_you_do": {"type": "string"},
            "how_you_keep_context": {"type": "string"},
            "closeout_rules": {"type": "string", "description": "Companion-specific instructions added to the standard closeout procedure."},
        },
        ["profile_id"],
    ),
    # update_own_description intentionally omitted. The implementation stays
    # for legacy administrative callers, but is not registered with MCP.
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
        "search_context",
        "Search Durable Context",
        "Search memories, profile stores, and joined shared projects together."
        " Every result is labeled with its source.",
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
        "set_messages_read_status",
        "Set Messages Read Status",
        "Set the read state for 1–200 of your inbox messages. Set read=true after"
        " handling messages; use read=false to restore messages to the unread inbox."
        " The explicit state is safe to retry.",
        {
            "profile_id": _PROFILE_ID,
            "message_ids": {
                "type": "array", "items": {"type": "string"},
                "minItems": 1, "maxItems": 200, "uniqueItems": True,
                "description": "Distinct inbox message ids.",
            },
            "read": {"type": "boolean"},
        },
        ["profile_id", "message_ids", "read"],
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
        "get_ironsworn_resource",
        "Get Ironsworn Resource",
        "Read one authoritative Ironsworn resource. For a move or oracle, read its"
        " stored index at session start and pass its exact indexed name; do not"
        " reconstruct rules or tables from memory. For sheet, omit name; it returns"
        " Oak's complete editable JSON sheet and applies no game rules.",
        {
            "profile_id": _PROFILE_ID,
            "resource": {
                "type": "string", "enum": ["move", "oracle", "sheet"],
                "description": "Resource to read.",
            },
            "name": {
                "type": "string", "minLength": 1,
                "description": "Exact indexed move or oracle name; omit for sheet.",
            },
        },
        ["profile_id", "resource"],
    ),
    _tool(
        "update_ironsworn_sheet",
        "Update Ironsworn Sheet",
        "Set exact existing sheet values by dotted path. Every value is editable."
        " Applies no arithmetic, caps, burn behavior, move outcomes, or other rules.",
        {
            "profile_id": _PROFILE_ID,
            "updates": {
                "type": "object",
                "additionalProperties": True,
                "description": "Exact assignments such as {'momentum': 3, 'vows.find_joy.ticks': 2}.",
            },
        },
        ["profile_id", "updates"],
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
        "Call with only profile_id to prepare a closeout and receive a one-time code. "
        "Then call again with code, facts, texture, exchange, and optional notes to persist it. "
        "The code is profile-bound, expires in 30 minutes, and is single-use.",
        {
            "profile_id": _PROFILE_ID,
            "code": {"type": "string", "minLength": 1},
            "facts": {"type": "string", "maxLength": 1200},
            "texture": {"type": "string", "maxLength": 700,
                        "description": "Concrete cues that help the next session retain rapport and tone; not a generic adjective. Preserve only what remains relevant."},
            "exchange": {"type": "string", "maxLength": 800,
                         "description": "Verbatim 1–3-turn excerpt; never paraphrase or paste a transcript."},
            "notes": {"type": "string", "maxLength": 700, "default": ""},
        },
        [],
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
        "Propose a dynamic structured store. Re-submit the same pending name to revise your own proposal and issue a fresh approval. Backend approval is required before records can be written.",
        {
            "profile_id": _PROFILE_ID,
            "name": {
                "type": "string",
                "description": "Lowercase store slug, for example hotel_reservations.",
            },
            "purpose": {"type": "string"},
            "schema": {
                "$ref": "#/$defs/DynamicSchema",
                "description": "Dynamic-store schema: {'fields': {name: {'type': 'string|number|integer|boolean|date', 'required': true|false}}}.",
            },
        },
        ["profile_id", "name", "purpose", "schema"],
    ),
    _tool(
        "query_records",
        "Query Records",
        "Search records in an approved or archived dynamic store. contains is a free-text substring search; where is a structured field filter. When both are supplied, both must match. Fields, sort, and projection are optional.",
        {
            "profile_id": _PROFILE_ID,
            "store_name": {"type": "string"},
            "contains": {"type": "string", "default": ""},
            "where": {"type": "object", "default": {}, "additionalProperties": {
                "anyOf": [
                    {"not": {"type": "object"}}, {"type": "object", "properties": {
                        "eq": {}, "ne": {}, "gt": {}, "gte": {}, "lt": {}, "lte": {},
                        "contains": {}, "in": {"type": "array"},
                    }, "additionalProperties": False, "minProperties": 1, "maxProperties": 1}
                ]}},
            "fields": {"type": "array", "items": {"type": "string"}},
            "order_by": {"type": "string"},
            "descending": {"type": "boolean", "default": True},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
        ["profile_id", "store_name"],
    ),
    _tool(
        "exam_attempt", "Exam Attempt",
        "LT Rita only. Use action draw to create a weighted attempt, grade to score a live attempt, or correct_grade to auditably correct a graded attempt and replay its learning statistics. Each action accepts only its relevant fields.",
        {"companion_name": {"type": "string", "enum": ["lt_rita"]},
         "action": {"type": "string", "enum": ["draw", "grade", "correct_grade"]},
         "where": {"type": "object", "additionalProperties": {
             "anyOf": [
                 {"not": {"type": "object"}}, {"type": "object", "properties": {
                     "eq": {}, "ne": {}, "gt": {}, "gte": {}, "lt": {}, "lte": {},
                     "contains": {}, "in": {"type": "array"},
                 }, "additionalProperties": False, "minProperties": 1, "maxProperties": 1}
             ]}},
         "count": {"type": "integer", "minimum": 1, "maximum": 20},
         "attempt_code": {"type": "string", "minLength": 1},
         "answers": {"type": "array", "minItems": 1, "items": {
             "type": "object", "properties": {
                 "position": {"type": "integer", "minimum": 1, "maximum": 20},
                 "selected": {"type": "array", "minItems": 1,
                              "items": {"type": "string", "enum": ["A", "B", "C", "D", "E"]}},
             }, "required": ["position", "selected"], "additionalProperties": False}},
         "reason": {"type": "string", "minLength": 1}},
        ["companion_name", "action"],
    ),
    _tool(
        "exam_review", "Exam Review",
        "LT Rita only. Use action diagnose_weaknesses for aggregate performance analysis, or revise_answer_key to auditably override, nullify, or restore a question answer key. Each action accepts only its relevant fields.",
        {"companion_name": {"type": "string", "enum": ["lt_rita"]},
         "action": {"type": "string", "enum": ["diagnose_weaknesses", "revise_answer_key"]},
         "attempt_code": {"type": "string", "minLength": 1},
         "position": {"type": "integer", "minimum": 1, "maximum": 20},
         "answer_action": {"type": "string", "enum": ["override", "nullify", "restore_extracted"]},
         "selected": {"type": "array", "items": {"type": "string", "enum": ["A", "B", "C", "D", "E"]}},
         "reason": {"type": "string", "minLength": 1}},
        ["companion_name", "action"],
    ),
    _tool(
        "get_record", "Get Record",
        "Read one record by ID, optionally returning only selected data fields.",
        {"profile_id": _PROFILE_ID, "store_name": {"type": "string"},
         "record_id": {"type": "string"},
         "fields": {"type": "array", "items": {"type": "string"}}},
        ["profile_id", "store_name", "record_id"],
    ),
    _tool(
        "update_record", "Update Record",
        "Patch selected fields in one record. The complete result is validated against its schema version.",
        {"profile_id": _PROFILE_ID, "store_name": {"type": "string"},
         "record_id": {"type": "string"}, "patch": {"type": "object"}},
        ["profile_id", "store_name", "record_id", "patch"],
    ),
    _tool(
        "delete_record", "Delete Record", "Delete one record by ID.",
        {"profile_id": _PROFILE_ID, "store_name": {"type": "string"},
         "record_id": {"type": "string"}},
        ["profile_id", "store_name", "record_id"],
    ),
    _tool(
        "add_records", "Add Records",
        "Add 1–200 schema-validated records atomically to an approved store. For thread_continuity, pass exactly one record; it upserts by source_type and source_id.",
        {"profile_id": _PROFILE_ID, "store_name": {"type": "string"}, "records": {"type": "array", "items": {"type": "object"}, "minItems": 1, "maxItems": 200}},
        ["profile_id", "store_name", "records"],
    ),
]

MCP_TOOL_NAMES = {tool["name"] for tool in MCP_TOOLS}


def _advertised_tools() -> list[dict[str, Any]]:
    # ChatGPT's connector setup rejects the full tool list when it cannot
    # validate an output schema.  Keep discovery reliably available by
    # default; operators with a host that supports output schemas can opt in
    # with MCP_OMIT_OUTPUT_SCHEMAS=0.
    if os.environ.get("MCP_OMIT_OUTPUT_SCHEMAS", "1") == "0":
        return MCP_TOOLS
    return [{k: v for k, v in tool.items() if k != "outputSchema"} for tool in MCP_TOOLS]


@dataclass
class MCPSettings:
    auth_required: bool = True
    connector_tokens: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    allow_any_origin: bool = False
    public_base_url: str | None = None
    oauth_issuer: str | None = None
    oauth_signing_key: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # Keep this secret stable across deploys. It signs 30-minute MCP-only
    # closeout preparation codes; it is intentionally distinct from backend
    # credentials and may default to the OAuth signing key for compatibility.
    closeout_signing_key: str | None = None
    # Put this on a persistent volume in deployment so consumed-code replay
    # protection survives an MCP process/container restart.
    closeout_code_state_file: str | None = None
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
            closeout_signing_key=(
                os.environ.get("MCP_CLOSEOUT_SIGNING_KEY")
                or os.environ.get("MCP_OAUTH_SIGNING_KEY")
                or os.environ.get("MCP_CONNECTOR_TOKEN")
                or None
            ),
            closeout_code_state_file=(
                os.environ.get("MCP_CLOSEOUT_CODE_STATE_FILE")
                or ((os.environ.get("MCP_OAUTH_STATE_FILE") + ".closeout.sqlite3")
                    if os.environ.get("MCP_OAUTH_STATE_FILE") else None)
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

    def exchange_code(
        self,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
    ) -> tuple[OAuthCode | None, str | None]:
        """Validate and consume an authorization code atomically.

        OAuth clients may probe token authentication styles before retrying
        with public-client credentials in the body. A failed probe must not
        burn the one-time code; only a fully valid exchange does.
        """
        with self._lock:
            item = self._codes.get(code)
            if item is None or item.expires_at < time.time():
                return None, "invalid_grant"
            if client_id != item.client_id:
                return None, "invalid_client"
            if redirect_uri != item.redirect_uri:
                return None, "invalid_grant"
            if not hmac.compare_digest(code_challenge, item.code_challenge):
                return None, "invalid_grant"
            self._codes.pop(code, None)
            return item, None


class CloseoutCodeState:
    """Durable, one-time closeout-code reservations.

    The code itself is signed and therefore survives an MCP process restart as
    long as its signing key remains stable.  This SQLite ledger is deliberately
    separate from the Profile OS backend: MCP preparation is an adapter-only
    concern, while the existing HTTP prepare/closeout routes remain unchanged.
    A lease prevents two callers from persisting the same closeout concurrently;
    a failed backend write releases the lease, while a successful write consumes
    it.  Deployments must put this file on persistent storage (and keep
    ``MCP_CLOSEOUT_SIGNING_KEY`` stable) if they restart during the 30-minute
    code lifetime.
    """

    def __init__(self, state_file: str):
        self.state_file = state_file
        parent = os.path.dirname(state_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._connection = sqlite3.connect(state_file, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS mcp_closeout_codes (
                digest TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('leased', 'consumed')),
                updated_at INTEGER NOT NULL
            )"""
        )
        self._connection.commit()
        self._lock = threading.Lock()

    def acquire(self, digest: str, expires_at: int) -> bool:
        """Atomically lease an unused code, returning False on replay."""
        now = int(time.time())
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM mcp_closeout_codes WHERE expires_at <= ?", (now,))
                row = connection.execute(
                    "SELECT state FROM mcp_closeout_codes WHERE digest=?", (digest,)
                ).fetchone()
                if row is not None:
                    connection.rollback()
                    return False
                connection.execute(
                    "INSERT INTO mcp_closeout_codes(digest, expires_at, state, updated_at) "
                    "VALUES (?, ?, 'leased', ?)",
                    (digest, expires_at, now),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def release(self, digest: str) -> None:
        """Release a lease after a failed backend write; it was not consumed."""
        with self._lock:
            self._connection.execute(
                "DELETE FROM mcp_closeout_codes WHERE digest=? AND state='leased'", (digest,)
            )
            self._connection.commit()

    def consume(self, digest: str) -> None:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE mcp_closeout_codes SET state='consumed', updated_at=? "
                "WHERE digest=? AND state='leased'",
                (int(time.time()), digest),
            )
            self._connection.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("closeout code lease was lost before consumption")


class MCPToolRunner:
    def __init__(
        self,
        bridge: ToolBridge,
        *,
        closeout_signing_key: str,
        closeout_code_state: CloseoutCodeState,
    ):
        self.bridge = bridge
        self._closeout_signing_key = closeout_signing_key.encode("utf-8")
        self._closeout_code_state = closeout_code_state

    def _prepare_closeout(self, profile_id: str) -> dict[str, Any]:
        prepared = self.bridge.prepare_closeout(profile_id)
        expires_at = int(time.time()) + 30 * 60
        payload = {
            "v": 1,
            "p": profile_id,
            "exp": expires_at,
            "n": secrets.token_urlsafe(18),
        }
        encoded = _b64url(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
        signature = _b64url(hmac.new(
            self._closeout_signing_key, encoded.encode("ascii"), hashlib.sha256,
        ).digest())
        return {
            "phase": "prepared",
            "profile_id": profile_id,
            "code": f"poscloseout.v1.{encoded}.{signature}",
            "expires_at": expires_at,
            "instructions": prepared["instructions"],
        }

    def _decode_closeout_code(self, code: Any) -> tuple[str, int, str]:
        if not isinstance(code, str):
            raise ToolBridgeError(400, "closeout code must be a string")
        parts = code.split(".")
        if len(parts) != 4 or parts[:2] != ["poscloseout", "v1"]:
            raise ToolBridgeError(400, "invalid closeout code")
        encoded, supplied_signature = parts[2], parts[3]
        expected_signature = _b64url(hmac.new(
            self._closeout_signing_key, encoded.encode("ascii"), hashlib.sha256,
        ).digest())
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise ToolBridgeError(400, "invalid closeout code")
        try:
            payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
            profile_id = payload["p"]
            expires_at = int(payload["exp"])
            nonce = payload["n"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            raise ToolBridgeError(400, "invalid closeout code") from None
        if (payload.get("v") != 1 or not isinstance(profile_id, str)
                or not profile_id or not isinstance(nonce, str) or not nonce):
            raise ToolBridgeError(400, "invalid closeout code")
        if expires_at <= int(time.time()):
            raise ToolBridgeError(400, "closeout code has expired; prepare again")
        return profile_id, expires_at, hashlib.sha256(code.encode("utf-8")).hexdigest()

    def _complete_closeout(self, arguments: dict[str, Any]) -> dict[str, Any]:
        profile_id, expires_at, digest = self._decode_closeout_code(arguments["code"])
        if not self._closeout_code_state.acquire(digest, expires_at):
            raise ToolBridgeError(409, "closeout code has already been used or is in progress")
        try:
            closeout = self.bridge.closeout(
                profile_id,
                arguments["facts"],
                arguments["texture"],
                arguments["exchange"],
                arguments.get("notes", ""),
            )
        except Exception:
            self._closeout_code_state.release(digest)
            raise
        self._closeout_code_state.consume(digest)
        return {"phase": "closed", "closeout": closeout}

    @staticmethod
    def _require_action_contract(
        tool: str,
        action: str,
        arguments: dict[str, Any],
        *,
        required: set[str],
        allowed: set[str],
    ) -> None:
        """Reject action fields that would otherwise be silently ignored.

        MCP's flat input schemas intentionally advertise the union of an
        action tool's fields.  Enforcing the exact per-action contract here
        keeps calls unambiguous even from clients that do not validate the
        schema before dispatch.
        """
        keys = set(arguments)
        missing = sorted(required - keys)
        if missing:
            raise ToolBridgeError(
                400, f"{tool} action {action!r} requires: {', '.join(missing)}")
        irrelevant = sorted(keys - allowed)
        if irrelevant:
            raise ToolBridgeError(
                400, f"{tool} action {action!r} does not accept: {', '.join(irrelevant)}")

    def _exam_attempt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = arguments.get("action")
        contracts = {
            "draw": (
                {"companion_name", "action"},
                {"companion_name", "action", "where", "count"},
            ),
            "grade": (
                {"companion_name", "action", "attempt_code", "answers"},
                {"companion_name", "action", "attempt_code", "answers"},
            ),
            "correct_grade": (
                {"companion_name", "action", "attempt_code", "answers", "reason"},
                {"companion_name", "action", "attempt_code", "answers", "reason"},
            ),
        }
        if action not in contracts:
            raise ToolBridgeError(400, "exam_attempt action must be draw, grade, or correct_grade")
        required, allowed = contracts[action]
        self._require_action_contract(
            "exam_attempt", action, arguments, required=required, allowed=allowed)
        companion_name = arguments["companion_name"]
        if action == "draw":
            result = self.bridge.draw_exam_questions(
                companion_name, where=arguments.get("where"),
                count=int(arguments.get("count", 1)))
        elif action == "grade":
            result = self.bridge.grade_exam_questions(
                companion_name, arguments["attempt_code"], arguments["answers"])
        else:
            result = self.bridge.regrade_exam_questions(
                companion_name, arguments["attempt_code"], arguments["answers"],
                arguments["reason"])
        return {"action": action, "result": result}

    def _exam_review(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = arguments.get("action")
        if action == "diagnose_weaknesses":
            self._require_action_contract(
                "exam_review", action, arguments,
                required={"companion_name", "action"},
                allowed={"companion_name", "action"})
            result = self.bridge.diagnose_exam_weaknesses(arguments["companion_name"])
        elif action == "revise_answer_key":
            self._require_action_contract(
                "exam_review", action, arguments,
                required={"companion_name", "action", "attempt_code", "position",
                          "answer_action", "reason"},
                allowed={"companion_name", "action", "attempt_code", "position",
                         "answer_action", "selected", "reason"})
            answer_action = arguments["answer_action"]
            if answer_action not in {"override", "nullify", "restore_extracted"}:
                raise ToolBridgeError(
                    400, "answer_action must be override, nullify, or restore_extracted")
            if answer_action == "override" and "selected" not in arguments:
                raise ToolBridgeError(400, "override requires selected")
            if answer_action != "override" and "selected" in arguments:
                raise ToolBridgeError(
                    400, f"{answer_action} does not accept selected")
            result = self.bridge.revise_exam_question_answer(
                arguments["companion_name"], arguments["attempt_code"],
                int(arguments["position"]), answer_action, arguments["reason"],
                arguments.get("selected"))
        else:
            raise ToolBridgeError(
                400, "exam_review action must be diagnose_weaknesses or revise_answer_key")
        return {"action": action, "result": result}

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        # No direct resolve_companion dispatch: resolution is private to the
        # summon_companion fallback below.
        if name == "discover_companions":
            return self.bridge.list_profiles()
        if name == "summon_companion":
            mode = arguments.get("mode", "conversation")
            if not isinstance(mode, str) or mode not in {"conversation", "forum"}:
                raise ValueError("mode must be 'conversation' or 'forum'")
            forum_mode = mode == "forum"
            requested = arguments["profile_id"]
            try:
                # Canonical ids are decisive and incur no directory lookup.
                session = self.bridge.start_session(requested)
            except ToolBridgeError as error:
                if error.status_code != 404:
                    raise
            else:
                if forum_mode:
                    session = self._with_forum_continuity(session)
                return self._with_tool_probe_catalog(session)
            resolution = self.bridge.resolve_companion(requested)
            if resolution["status"] == "resolved":
                session = self.bridge.start_session(resolution["resolved_profile_id"])
                if forum_mode:
                    session = self._with_forum_continuity(session)
                return self._with_tool_probe_catalog(session)
            ids = [profile["id"] for profile in resolution["candidates"]]
            if resolution["status"] == "ambiguous":
                raise ToolBridgeError(
                    409,
                    f"ambiguous companion {requested!r}; candidates: {', '.join(ids)}",
                )
            raise ToolBridgeError(404, f"no companion matches {requested!r}")
        if name == "propose_prompt_edit":
            prompt_args = {key: arguments[key] for key in (
                "who_you_are", "signature", "lane", "voice", "what_you_do",
                "how_you_keep_context", "closeout_rules") if key in arguments}
            return self.bridge.propose_prompt_edit(arguments["profile_id"], **prompt_args)
        # No update_own_description dispatch: legacy implementation retained
        # outside the companion-callable MCP registry.
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
        if name == "search_context":
            return self.bridge.search_context(
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
        if name == "set_messages_read_status":
            return self.bridge.set_messages_read_status(
                arguments["profile_id"], arguments["message_ids"], arguments["read"])
        if name == "write_file":
            return self.bridge.write_file(
                arguments["profile_id"], arguments["filename"], arguments["content"])
        if name == "list_files":
            return self.bridge.list_files(arguments["profile_id"])
        if name == "read_file":
            return self.bridge.read_file(arguments["profile_id"], arguments["filename"])
        if name == "get_ironsworn_resource":
            resource = arguments["resource"]
            resource_name = arguments.get("name")
            if resource in {"move", "oracle"}:
                if not isinstance(resource_name, str) or not resource_name.strip():
                    raise ToolBridgeError(400, f"name is required for Ironsworn {resource}")
                getter = (self.bridge.get_ironsworn_move if resource == "move"
                          else self.bridge.get_ironsworn_oracle)
                item = getter(arguments["profile_id"], resource_name)
            elif resource == "sheet":
                if resource_name is not None:
                    raise ToolBridgeError(400, "name must be omitted for Ironsworn sheet")
                item = self.bridge.get_ironsworn_sheet(arguments["profile_id"])
            else:
                raise ToolBridgeError(400, "resource must be one of: move, oracle, sheet")
            return {"resource": resource, "item": item}
        if name == "update_ironsworn_sheet":
            return self.bridge.update_ironsworn_sheet(
                arguments["profile_id"], arguments["updates"])
        if name == "delete_file":
            return self.bridge.delete_file(arguments["profile_id"], arguments["filename"])
        if name == "closeout":
            keys = set(arguments)
            if keys == {"profile_id"}:
                return self._prepare_closeout(arguments["profile_id"])
            completion_required = {"code", "facts", "texture", "exchange"}
            completion_allowed = completion_required | {"notes"}
            if completion_required <= keys and keys <= completion_allowed:
                return self._complete_closeout(arguments)
            raise ToolBridgeError(
                400,
                "closeout accepts exactly {profile_id} to prepare, or "
                "{code, facts, texture, exchange, notes?} to persist",
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
            return self.bridge.query_records(
                arguments["profile_id"],
                arguments["store_name"],
                contains=arguments.get("contains") or None,
                where=arguments.get("where"),
                fields=arguments.get("fields"),
                order_by=arguments.get("order_by"),
                descending=bool(arguments.get("descending", True)),
                limit=int(arguments.get("limit", 50)),
            )
        if name == "exam_attempt":
            return self._exam_attempt(arguments)
        if name == "exam_review":
            return self._exam_review(arguments)
        if name == "get_record":
            return self.bridge.get_record(arguments["profile_id"], arguments["store_name"],
                                          arguments["record_id"], arguments.get("fields"))
        if name == "update_record":
            return self.bridge.update_record(arguments["profile_id"], arguments["store_name"],
                                             arguments["record_id"], arguments["patch"])
        if name == "delete_record":
            return self.bridge.delete_record(arguments["profile_id"], arguments["store_name"],
                                             arguments["record_id"])
        if name == "add_records":
            return self.bridge.add_records(
                arguments["profile_id"], arguments["store_name"], arguments["records"])
        raise ValueError(f"unknown tool {name!r}")

    def _with_forum_continuity(self, session: dict[str, Any]) -> dict[str, Any]:
        """Add a bounded durable slice; The Thread joins exact post context."""
        profile_id = session["selection"]["profile_id"]
        rows = self.bridge.query_records(
            profile_id, "thread_continuity", limit=200)
        active = [row for row in rows if row.get("data", {}).get("status") == "active"]
        active.sort(
            key=lambda row: (
                bool(row["data"].get("open_loop")),
                row["data"].get("occurred_at", ""),
            ),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        used_chars = 0
        for row in active:
            item = {"record_id": row["id"], **row["data"]}
            item_chars = len(json.dumps(item, ensure_ascii=False))
            if used_chars + item_chars > 16_000:
                continue
            selected.append(item)
            used_chars += item_chars
            if len(selected) == 12:
                break
        return {
            **session,
            "identity": None,
            "thread_continuity": selected,
            "thread_continuity_write_contract": {
                "upsert": {
                    "tool": "add_records",
                    "arguments": {
                        "profile_id": profile_id,
                        "store_name": "thread_continuity",
                        "records": ["<complete continuity row>"],
                    },
                    "idempotency_key": ["source_type", "source_id"],
                },
                "status_update": {
                    "tool": "update_record",
                    "arguments": {
                        "profile_id": profile_id,
                        "store_name": "thread_continuity",
                        "record_id": "<thread_continuity[].record_id>",
                        "patch": {"status": "resolved|superseded"},
                    },
                },
            },
        }

    @staticmethod
    def _with_tool_probe_catalog(session: dict[str, Any]) -> dict[str, Any]:
        """Attach both server tool surfaces only to the diagnostic profile."""
        if session.get("selection", {}).get("profile_id") != "tool_probe":
            return session
        advertised = _advertised_tools()
        return {
            **session,
            "server_tool_catalog": {
                # This is the complete registry accepted by tools/call, not
                # the cropped tools/list response supplied to an MCP host.
                "registered_tools": deepcopy(MCP_TOOLS),
                "registered_tool_names": sorted(MCP_TOOL_NAMES),
                "mcp_advertised_tools": deepcopy(advertised),
                "mcp_advertised_tool_names": [tool["name"] for tool in advertised],
                "notes": (
                    "registered_tools is the complete server-side tools/call registry. "
                    "mcp_advertised_tools is this server's exact tools/list response."
                ),
            },
        }


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
        # Error results deliberately do not conform to a tool's successful
        # outputSchema; clients can rely on this common envelope instead.
        "structuredContent": {"error": {"message": message, "status": None}},
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
        LOGGER.warning("mcp 401: no bearer header (%s %s)",
                       request.method, request.url.path)
        return _unauthorized(settings, request)
    token = header[len("Bearer "):]
    for expected in settings.connector_tokens:
        if hmac.compare_digest(token, expected):
            return None
    if _validate_oauth_token(settings, request, token):
        return None
    LOGGER.warning("mcp 401: bearer token rejected (%s %s)",
                   request.method, request.url.path)
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
            # The server has no live notification stream (GET /mcp closes
            # immediately), so it can never deliver tools/list_changed —
            # advertising it would be dishonest and confuses strict hosts.
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "profile-os-mcp",
                "title": "Profile OS Remote MCP",
                "version": SERVER_VERSION,
            },
            "instructions": (
                "Call summon_companion with the companion phrase the user supplied. It tries "
                "a normalized exact canonical id first and only resolves names after a 404; "
                "do not browse the directory first. A returned selection identifies the active "
                "companion. Use discover_companions only for browsing "
                "or not_found results. The returned "
                "allowed_tools is guidance for which tools this profile should use; it is "
                "not enforced server-side."
            ),
        })

    if method == "ping":
        return _rpc_result(request_id, {})

    if method == "tools/list":
        return _rpc_result(request_id, {"tools": _advertised_tools()})

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
                {**_tool_error(f"backend returned {e.status_code}: {e.detail}"),
                 "structuredContent": {"error": {"message": f"backend returned {e.status_code}: {e.detail}",
                                                   "status": e.status_code}}},
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
    closeout_state_file = settings.closeout_code_state_file or os.path.join(
        "/tmp", "profile-os-mcp-closeout-codes.sqlite3")
    app.state.runner = MCPToolRunner(
        bridge or ToolBridge(),
        closeout_signing_key=settings.closeout_signing_key or settings.oauth_signing_key,
        closeout_code_state=CloseoutCodeState(closeout_state_file),
    )
    app.state.admin_verify = admin_verify or default_admin_verify
    _max_request_bytes = configured_max_request_bytes()
    _authorize_hits: dict[str, list[float]] = {}
    _approval_hits: dict[str, list[float]] = {}
    _create_profile_hits: dict[str, list[float]] = {}
    _session_inspector_hits: dict[str, list[float]] = {}
    _oauth_register_hits: dict[str, list[float]] = {}

    @app.middleware("http")
    async def _bounded_body(request: Request, call_next):
        try:
            body = await read_request_body(request, _max_request_bytes)
        except RequestBodyTooLarge:
            return JSONResponse(
                {"error": "request_too_large",
                 "max_request_bytes": _max_request_bytes},
                status_code=413,
            )
        replay_request_body(request, body)
        return await call_next(request)

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
    @app.get("/.well-known/openid-configuration")
    async def oauth_authorization_server_metadata(request: Request):
        issuer = _issuer_url(settings, request)
        metadata = {
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
        # ChatGPT probes OpenID discovery too.  Returning only RFC 8414
        # authorization-server metadata at this URL is not a valid OpenID
        # discovery document: these two fields are required by OIDC Discovery
        # even when the client is using plain OAuth (as this MCP flow does).
        if request.url.path.endswith("/openid-configuration"):
            metadata.update({
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["HS256"],
            })
        return metadata

    @app.post("/oauth/register", status_code=201)
    async def oauth_register(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        if _rate_limited(_oauth_register_hits, client_ip):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
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
        if _rate_limited(_authorize_hits, client_ip):
            return HTMLResponse("Too many attempts; try again in a minute.",
                                status_code=429)

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
            return HTMLResponse(f"Approval not found: {e.detail}", status_code=e.status_code,
                                headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})
        if approval.get("status") != "pending":
            return HTMLResponse(f"Already {approval.get('status')}. Nothing to do.",
                                headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})
        return HTMLResponse(_approval_page(approval),
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    @app.get("/session-inspector")
    async def session_inspector_page():
        profiles = await run_in_threadpool(app.state.runner.bridge.list_profiles)
        return HTMLResponse(_session_inspector_page(profiles))

    @app.post("/session-inspector")
    async def session_inspector_submit(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        if _rate_limited(_session_inspector_hits, client_ip):
            return HTMLResponse("Too many attempts; try again in a minute.", status_code=429)
        form = await request.form()
        profile_id = str(form.get("profile_id") or "")
        totp_code = str(form.get("totp_code") or "")
        mode = "raw" if str(form.get("mode")) == "raw" else "human"
        profiles = await run_in_threadpool(app.state.runner.bridge.list_profiles)
        if profile_id not in {str(p.get("id")) for p in profiles}:
            return HTMLResponse(_session_inspector_page(profiles, selected_id=profile_id,
                                                        mode=mode, error="Choose a valid companion."), status_code=422)
        try:
            result = await run_in_threadpool(app.state.runner.bridge.inspect_session,
                                              profile_id, totp_code)
        except ToolBridgeError as e:
            return HTMLResponse(_session_inspector_page(profiles, selected_id=profile_id,
                                                        mode=mode, error=e.detail), status_code=e.status_code)
        return HTMLResponse(_session_inspector_page(profiles, selected_id=profile_id,
                                                    mode=mode, result=result))

    @app.post("/approvals/{approval_id}")
    async def approval_decide(approval_id: str, request: Request):
        client_ip = request.client.host if request.client else "unknown"
        if _rate_limited(_approval_hits, client_ip):
            return HTMLResponse("Too many attempts; try again in a minute.",
                                status_code=429)

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
        if _rate_limited(_create_profile_hits, client_ip):
            return HTMLResponse("Too many attempts; try again in a minute.",
                                status_code=429)

        form = await request.form()
        values = {k: str(form.get(k) or "") for k in
                 ("id", "display_name", "description", "signature", "who_you_are", "what_you_do", "profile_kind")}
        totp_code = str(form.get("totp_code") or "")
        try:
            created = await run_in_threadpool(
                app.state.runner.bridge.create_profile_totp,
                values["id"], values["display_name"],
                values["who_you_are"], values["what_you_do"], totp_code,
                values["description"], values["signature"], values["profile_kind"] or "companion")
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
        verifier = str(data.get("code_verifier") or "")
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        code, error = app.state.oauth.exchange_code(
            str(data.get("code") or ""),
            str(data.get("client_id") or ""),
            str(data.get("redirect_uri") or ""),
            challenge if verifier else "",
        )
        if error is not None:
            return JSONResponse({"error": error}, status_code=400)
        assert code is not None
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

        # Phase 0 probe: log Mcp-Session-Id per request to verify whether
        # connectors present distinct ids per concurrent conversation.
        _probe_method = message.get("method")
        _probe_extra = ""
        if _probe_method == "tools/call":
            _params = message.get("params") or {}
            _probe_extra = (
                f" tool={_params.get('name')!r}"
                f" profile_id={(_params.get('arguments') or {}).get('profile_id')!r}"
            )
        LOGGER.info(
            "mcp_probe session_id=%r method=%r%s",
            request.headers.get("mcp-session-id"),
            _probe_method,
            _probe_extra,
        )

        response = _handle_rpc(message, app)
        accept = request.headers.get("accept", "")
        if "text/event-stream" in accept:
            # ChatGPT's Streamable HTTP client is tested against the reference
            # servers, which frame POST responses as a single SSE message when
            # the client accepts text/event-stream; plain JSON bodies have
            # produced intermittent client-side stream errors.
            body = f"event: message\ndata: {json.dumps(response)}\n\n"
            return Response(
                content=body,
                media_type="text/event-stream",
                headers=headers,
            )
        return JSONResponse(response, headers=headers)

    return app


app = create_mcp_app()
