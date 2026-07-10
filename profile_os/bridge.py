"""Tool bridge for local runners and the remote MCP adapter.

A thin client that exposes the backend's operational endpoints as named
tools with JSON-schema inputs (`TOOLS`), callable via `ToolBridge.call()`.
The deployable remote MCP server in `profile_os.mcp_server` reuses this
client while exposing a narrower Claude-facing tool set. See TOOL_BRIDGE.md.

Auth stance (see ACCESS_CONTROL.md): the bridge owns the credential, not
the model and not a profile. The secret comes from env/config and is sent
as `Authorization: Bearer <secret>` on every request; the backend does all
authorization. Nothing here bypasses or weakens route enforcement — a
missing grant surfaces as the backend's own 401/403. No LLM calls, no
storage, no business logic.

Env:
    PROFILE_OS_BRIDGE_BASE_URL  backend base URL (default http://127.0.0.1:8000)
    PROFILE_OS_BRIDGE_BEARER    bearer secret (optional when auth is disabled)
    PROFILE_OS_BRIDGE_KEY_ID          Ed25519 credential id (alternative to bearer)
    PROFILE_OS_BRIDGE_PRIVATE_KEY     base64 raw 32-byte Ed25519 private key

When both a bearer secret and a keypair are configured, the bearer wins
(it is the simpler, more common bridge setup). The keypair path exists for
self-enrolled `agent` principals from PLAN_AGENT_ENROLLMENT.md, which only
ever hold a signing key, not a shared secret.

Admin lifecycle tools (approve/reject/archive) are deliberately NOT exposed:
a bridge credential is operational, never `stores:approve`.
"""

from __future__ import annotations

import base64
import json
import os

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .sign import sign_request

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class ToolBridgeError(Exception):
    """Backend refused or failed the call; carries the HTTP status.

    401/403 mean the bridge credential is missing/invalid or lacks the
    grant for (operation, profile) — fix grants, don't retry.
    """

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"backend returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _tool(name, description, properties, required):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required}}


_PID = {"type": "string", "description": "assistant profile id, e.g. 'tara'"}

MEMORY_KINDS = ["decision", "fact", "failure_scar", "note", "observation",
                "preference"]

TOOLS = [
    _tool("whoami", "Canonical 'who am I talking to' identity file. Overrides"
                    " your memory on conflict — file wins, drift gets logged"
                    " by the human. Call this when unsure about personal facts.",
          {}, []),
    _tool("boot", "Boot a profile: compact state, prompts, recent memories.",
          {"profile_id": _PID}, ["profile_id"]),
    _tool("start_session", "Call this on your first response in a conversation instead"
                          " of boot: returns identity (whoami), prompts, compact_state,"
                          " the last 2 closeouts (not just the current state), and your"
                          " full memory history in one call.",
          {"profile_id": _PID}, ["profile_id"]),
    _tool("propose_prompt_edit", "Propose a change to your own base_prompt/role_prompt."
                                " Held pending until the human approves it with a live"
                                " TOTP code from their authenticator app — you cannot"
                                " approve your own edits.",
          {"profile_id": _PID,
           "base_prompt": {"type": "string"},
           "role_prompt": {"type": "string"}},
          ["profile_id"]),
    _tool("remember", "Append a memory event to a profile.",
          {"profile_id": _PID,
           "kind": {"type": "string", "enum": MEMORY_KINDS,
                    "description": "memory kind; use 'note' when unsure"},
           "content": {"type": "string"},
           "tags": {"type": "array", "items": {"type": "string"}}},
          ["profile_id", "kind", "content"]),
    _tool("search_memories", "Full-text search over a profile's memory events.",
          {"profile_id": _PID,
           "query": {"type": "string"},
           "limit": {"type": "integer", "default": 20}},
          ["profile_id", "query"]),
    _tool("closeout", "Close a session: log notes and set the new compact state.",
          {"profile_id": _PID,
           "notes": {"type": "string"},
           "new_state": {"type": "string"}},
          ["profile_id", "notes", "new_state"]),
    _tool("propose_store", "Propose a dynamic store (needs admin approval before writes).",
          {"profile_id": _PID,
           "name": {"type": "string"},
           "purpose": {"type": "string"},
           "schema": {"type": "object",
                      "description": "{'fields': {name: {'type': ..., 'required': ...}}}"}},
          ["profile_id", "name", "purpose", "schema"]),
    _tool("list_stores", "List a profile's dynamic store definitions.",
          {"profile_id": _PID}, ["profile_id"]),
    _tool("get_store", "Get one dynamic store definition (schema, status, version).",
          {"profile_id": _PID, "name": {"type": "string"}},
          ["profile_id", "name"]),
    _tool("add_record", "Add a schema-validated record to an approved dynamic store.",
          {"profile_id": _PID,
           "store_name": {"type": "string"},
           "data": {"type": "object"}},
          ["profile_id", "store_name", "data"]),
    _tool("query_records", "Query records of a dynamic store.",
          {"profile_id": _PID,
           "store_name": {"type": "string"},
           "contains": {"type": "string"},
           "limit": {"type": "integer", "default": 50}},
          ["profile_id", "store_name"]),
    _tool("audit", "Read the store lifecycle audit trail (profile-wide or one store).",
          {"profile_id": _PID,
           "store_name": {"type": "string"},
           "limit": {"type": "integer", "default": 100}},
          ["profile_id"]),
]


class ToolBridge:
    """HTTP client exposing backend endpoints as tools. 1:1, no logic.

    `client` injection exists for tests (any httpx.Client-compatible
    object, e.g. fastapi.testclient.TestClient); production callers rely
    on env/config.
    """

    def __init__(self, base_url: str | None = None, bearer: str | None = None,
                 client: httpx.Client | None = None, key_id: str | None = None,
                 private_key: Ed25519PrivateKey | None = None):
        self._base_url = (base_url
                          or os.environ.get("PROFILE_OS_BRIDGE_BASE_URL")
                          or DEFAULT_BASE_URL)
        self._bearer = bearer if bearer is not None else os.environ.get(
            "PROFILE_OS_BRIDGE_BEARER")
        self._key_id = key_id or os.environ.get("PROFILE_OS_BRIDGE_KEY_ID")
        self._private_key = private_key
        if self._private_key is None:
            raw = os.environ.get("PROFILE_OS_BRIDGE_PRIVATE_KEY")
            if raw:
                self._private_key = Ed25519PrivateKey.from_private_bytes(
                    base64.b64decode(raw))
        self._client = client or httpx.Client(base_url=self._base_url)

    def close(self):
        self._client.close()

    def _request(self, method: str, path: str, *, json: dict | None = None,
                 params: dict | None = None):
        headers = {}
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        elif self._private_key and self._key_id:
            body_bytes = b"" if json is None else __import__("json").dumps(json).encode()
            headers["Authorization"] = sign_request(
                self._private_key, self._key_id, method, path, body_bytes)
        r = self._client.request(method, path, json=json, params=params,
                                 headers=headers)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ToolBridgeError(r.status_code, detail)
        return r.json()

    # -- tools (names match TOOLS) -------------------------------------------

    def whoami(self):
        return self._request("GET", "/identity")

    def list_profiles(self):
        return self._request("GET", "/profiles")

    def boot_profile(self, profile_id: str):
        return self.boot(profile_id)

    def boot(self, profile_id: str):
        return self._request("POST", f"/profiles/{profile_id}/boot")

    def start_session(self, profile_id: str):
        return self._request("POST", f"/profiles/{profile_id}/session")

    def propose_prompt_edit(self, profile_id: str, base_prompt: str | None = None,
                            role_prompt: str | None = None):
        return self._request("POST", f"/profiles/{profile_id}/prompt",
                             json={"base_prompt": base_prompt, "role_prompt": role_prompt})

    def get_approval(self, approval_id: str):
        """Not an MCP tool: used by the mcp service's public /approvals/{id}
        link page, authenticated with this bridge's own bearer (which holds
        approvals:totp_decide) — the human never needs the bridge secret."""
        return self._request("GET", f"/approvals/{approval_id}")

    def decide_approval(self, approval_id: str, approve: bool,
                        totp_code: str | None = None):
        return self._request("POST", f"/approvals/{approval_id}/decide",
                             json={"approve": approve, "totp_code": totp_code})

    def remember(self, profile_id: str, kind: str, content: str,
                 tags: list[str] | None = None):
        return self._request("POST", f"/profiles/{profile_id}/memories",
                             json={"kind": kind, "content": content,
                                   "tags": tags or []})

    def search_memories(self, profile_id: str, query: str, limit: int = 20):
        return self._request("GET", f"/profiles/{profile_id}/memories/search",
                             params={"q": query, "limit": limit})

    def closeout(self, profile_id: str, notes: str, new_state: str):
        return self._request("POST", f"/profiles/{profile_id}/closeout",
                             json={"notes": notes, "new_state": new_state})

    def propose_store(self, profile_id: str, name: str, purpose: str,
                      schema: dict):
        return self._request("POST", f"/profiles/{profile_id}/stores",
                             json={"name": name, "purpose": purpose,
                                   "proposed_by": f"bridge:{profile_id}",
                                   "schema": schema})

    def list_stores(self, profile_id: str):
        return self._request("GET", f"/profiles/{profile_id}/stores")

    def get_store(self, profile_id: str, name: str):
        return self._request("GET", f"/profiles/{profile_id}/stores/{name}")

    def add_record(self, profile_id: str, store_name: str, data: dict):
        return self._request(
            "POST", f"/profiles/{profile_id}/stores/{store_name}/records",
            json={"data": data})

    def query_records(self, profile_id: str, store_name: str,
                      contains: str | None = None, limit: int = 50):
        return self._request(
            "GET", f"/profiles/{profile_id}/stores/{store_name}/records",
            params={"contains": contains, "limit": limit})

    def audit(self, profile_id: str, store_name: str | None = None,
              limit: int = 100):
        if store_name is None:
            return self._request("GET", f"/profiles/{profile_id}/audit",
                                 params={"limit": limit})
        return self._request(
            "GET", f"/profiles/{profile_id}/stores/{store_name}/audit",
            params={"limit": limit})

    # -- generic dispatch for local hosted-assistant runners ------------------

    def call(self, name: str, arguments: dict):
        if name not in {t["name"] for t in TOOLS}:
            raise ToolBridgeError(404, f"unknown tool {name!r}")
        return getattr(self, name)(**arguments)
