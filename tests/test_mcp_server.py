import base64
import hashlib
import logging
import urllib.parse

import httpx
from jsonschema import Draft202012Validator
from fastapi.testclient import TestClient

from profile_os.bootstrap_bridge import BRIDGE_OPS, bootstrap
from profile_os.access import AccessControl
from profile_os.bridge import ToolBridge, ToolBridgeError
from profile_os.mcp_server import MCPSettings, MCP_TOOLS, OAuthState, create_mcp_app
from profile_os.storage import Store


CONNECTOR_TOKEN = "claude-connector-token"
BACKEND_TOKEN = "profile-os-backend-token"
ORIGIN = "https://claude.ai"
PUBLIC_BASE = "https://profiles.example"


ThreadedASGIClient = TestClient


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _bearer(token: str = CONNECTOR_TOKEN) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Origin": ORIGIN,
        # Plain-JSON accept keeps r.json() usable across these tests; the
        # SSE framing real hosts negotiate is covered by
        # test_post_responses_use_sse_when_accepted.
        "Accept": "application/json",
    }


def _sse_json(r) -> dict:
    import json as _json
    assert r.headers["content-type"].startswith("text/event-stream")
    line = next(l for l in r.text.splitlines() if l.startswith("data: "))
    return _json.loads(line[len("data: "):])


def _rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    msg = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class FakeBridge:
    def __init__(self):
        self.memories = []
        self.stores = {}
        self.records = []
        self.store_approved = False
        self.approvals = {}

    def list_profiles(self):
        return [
            self._profile("sidra"), self._profile("tara"),
        ]

    @staticmethod
    def _profile(profile_id):
        return {"id": profile_id, "display_name": profile_id.title(),
                "description": "", "allowed_tools": ["remember", "search_memories"],
                "memory_policy": {"max_boot_events": 10},
                "closeout_rules": "Write compact state.", "created_at": 1}

    def boot_profile(self, profile_id: str):
        return {
            "profile": self._profile(profile_id),
            "base_prompt": "Base prompt.",
            "role_prompt": "Role prompt.",
            "compact_state": "No active task.",
            "state_updated_at": None, "recent_memories": list(self.memories),
        }

    def inspect_session(self, profile_id, totp_code):
        if totp_code != "123456":
            raise ToolBridgeError(401, "missing or invalid TOTP code")
        return {
            **self.boot_profile(profile_id),
            "identity": "Canonical identity.",
            "memories": [{"id": "memory-1", "kind": "note", "content": "A memory.", "tags": ["lookup-only"]}],
            "you_got_mail": False,
            "server_time": {"unix": 1, "iso": "1970-01-01T00:00:01+00:00"},
        }

    def remember(self, profile_id, kind, content, tags=None):
        event = {
            "id": "mem-1", "created_at": 1,
            "profile_id": profile_id,
            "kind": kind,
            "content": content,
            "tags": tags or [],
        }
        self.memories.append(event)
        return event

    def search_memories(self, profile_id, query, limit=20):
        return [m for m in self.memories if query.lower() in m["content"].lower()][:limit]

    def closeout(self, profile_id, facts, texture, exchange, notes=""):
        return {"id": "closeout-1", "profile_id": profile_id,
                "facts": facts, "texture": texture, "exchange": exchange,
                "notes": notes, "new_state": facts, "created_at": 1}

    def list_stores(self, profile_id):
        return list(self.stores.values())

    def propose_store(self, profile_id, name, purpose, schema):
        store = {"name": name, "purpose": purpose, "schema": schema,
                "status": "pending", "profile_id": profile_id,
                 "id": "store-1", "version": 1, "proposed_by": profile_id,
                 "rejection_reason": None, "created_at": 1, "approved_at": None,
                 "rejected_at": None, "approval_id": "store-approval-1"}
        self.stores[name] = store
        self.approvals[store["approval_id"]] = {
            "id": store["approval_id"],
            "kind": "store_schema",
            "profile_id": profile_id,
            "status": "pending",
            "payload": {
                "store_id": store["id"],
                "store_name": name,
                "purpose": purpose,
                "schema": schema,
            },
        }
        self.store_approved = False
        return store

    def query_records(self, profile_id, store_name, contains=None, limit=50):
        rows = [r for r in self.records if r["store"] == store_name]
        if contains:
            rows = [r for r in rows if contains.lower() in str(r["data"]).lower()]
        return rows[:limit]

    def add_record(self, profile_id, store_name, data):
        if not self.store_approved:
            raise ToolBridgeError(409, "store has no approved version")
        record = {"id": "rec-1", "store": store_name, "schema_version": 1,
                  "data": data, "created_at": 1, "updated_at": None}
        self.records.append(record)
        return record

    def propose_prompt_edit(self, profile_id, base_prompt=None, role_prompt=None):
        approval = {"id": "approval-1", "kind": "prompt_edit", "profile_id": profile_id,
                   "status": "pending",
                   "payload": {"base_prompt": base_prompt, "role_prompt": role_prompt}}
        self.approvals[approval["id"]] = approval
        return approval

    def get_approval(self, approval_id):
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise ToolBridgeError(404, "unknown approval")
        return approval

    def decide_approval(self, approval_id, approve, totp_code=None):
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise ToolBridgeError(404, "unknown approval")
        if approval["status"] != "pending":
            raise ToolBridgeError(409, f"approval already {approval['status']}")
        if approve and totp_code != "123456":
            raise ToolBridgeError(401, "missing or invalid TOTP code")
        approval["status"] = "approved" if approve else "rejected"
        if approval["kind"] == "store_schema" and approve:
            self.store_approved = True
        return approval

    def create_profile_totp(self, profile_id, display_name, base_prompt,
                            role_prompt, totp_code):
        if totp_code != "123456":
            raise ToolBridgeError(401, "missing or invalid TOTP code")
        if profile_id in {"sidra", "tara"}:
            raise ToolBridgeError(409, f"profile {profile_id!r} already exists")
        return {"id": profile_id, "display_name": display_name,
                "base_prompt": base_prompt, "role_prompt": role_prompt}


class RecordingHTTPClient:
    def __init__(self):
        self.requests = []

    def request(self, method, path, *, json=None, params=None, headers=None):
        self.requests.append({
            "method": method,
            "path": path,
            "json": json,
            "params": params,
            "headers": headers or {},
        })
        return httpx.Response(200, json={
            "profile": {"id": "tara", "display_name": "Tara"},
            "base_prompt": "Base prompt.",
            "role_prompt": "Role prompt.",
            "compact_state": "State.",
            "recent_memories": [],
        })

    def close(self):
        return None


def _mcp_client(bridge=None):
    bridge = bridge or FakeBridge()
    settings = MCPSettings(
        auth_required=True,
        connector_tokens=[CONNECTOR_TOKEN],
        allowed_origins=[ORIGIN],
        public_base_url=PUBLIC_BASE,
        oauth_issuer=PUBLIC_BASE,
        oauth_signing_key="test-signing-key",
    )
    return ThreadedASGIClient(create_mcp_app(bridge=bridge, settings=settings))


def _call_tool(client: ThreadedASGIClient, name: str, arguments: dict, request_id: int = 10):
    return client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": name, "arguments": arguments}, request_id),
        headers=_bearer(),
    )


def test_initialize_and_list_tools(tmp_path, monkeypatch):
    # Schema-capable clients can opt in to output schemas.
    monkeypatch.setenv("MCP_OMIT_OUTPUT_SCHEMAS", "0")
    client = _mcp_client()

    r = client.post(
        "/mcp",
        json=_rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test"},
        }),
        headers=_bearer(),
    )
    assert r.status_code == 200
    body = r.json()["result"]
    assert body["protocolVersion"] == "2025-06-18"
    assert body["capabilities"] == {"tools": {"listChanged": False}}
    assert "boot_profile" in body["instructions"]

    r = client.post("/mcp", json=_rpc("tools/list"), headers=_bearer())
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {tool["name"] for tool in MCP_TOOLS}
    assert not names & {"approve_store", "reject_store", "archive_store", "audit"}
    assert names == {tool["name"] for tool in MCP_TOOLS}
    for tool in tools:
        assert set(tool) == {"name", "title", "description", "inputSchema", "outputSchema", "annotations"}
        assert tool["outputSchema"]["type"] == "object"
        assert set(tool["annotations"]) == {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
    list_profiles = next(tool for tool in tools if tool["name"] == "list_profiles")
    assert list_profiles["outputSchema"]["properties"]["items"]["type"] == "array"
    closeout = next(tool for tool in tools if tool["name"] == "closeout")
    assert set(closeout["inputSchema"]["properties"]) == {
        "profile_id", "facts", "texture", "exchange", "notes",
    }
    assert closeout["inputSchema"]["required"] == [
        "profile_id", "facts", "texture", "exchange",
    ]
    assert closeout["inputSchema"]["properties"]["notes"]["maxLength"] == 700
    annotations = {tool["name"]: tool["annotations"] for tool in tools}
    assert annotations["forget"]["destructiveHint"] is True
    assert annotations["delete_file"]["destructiveHint"] is True
    assert annotations["delete_record"]["destructiveHint"] is True
    assert annotations["boot_profile"]["readOnlyHint"] is True
    assert annotations["leave_project"] == {"readOnlyHint": False, "destructiveHint": False,
                                             "idempotentHint": True, "openWorldHint": True}


def test_list_tools_can_omit_output_schemas(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_OMIT_OUTPUT_SCHEMAS", "1")
    client = _mcp_client()
    r = client.post("/mcp", json=_rpc("tools/list"), headers=_bearer())
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {tool["name"] for tool in MCP_TOOLS}
    for tool in tools:
        assert set(tool) == {"name", "title", "description", "inputSchema", "annotations"}


def test_list_tools_omit_output_schemas_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_OMIT_OUTPUT_SCHEMAS", raising=False)
    client = _mcp_client()
    r = client.post("/mcp", json=_rpc("tools/list"), headers=_bearer())
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    discovered = next(tool for tool in tools if tool["name"] == "list_profiles")
    assert "outputSchema" not in discovered


def test_post_responses_use_sse_when_accepted(tmp_path):
    import json as _json
    client = _mcp_client()
    headers = _bearer() | {"Accept": "application/json, text/event-stream"}
    r = client.post("/mcp", json=_rpc("ping"), headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    data_lines = [l for l in r.text.splitlines() if l.startswith("data: ")]
    assert len(data_lines) == 1
    body = _json.loads(data_lines[0][len("data: "):])
    assert body == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_mcp_tool_flow_and_logging(tmp_path, caplog):
    bridge = FakeBridge()
    client = _mcp_client(bridge)

    profiles = _call_tool(client, "list_profiles", {}).json()["result"]
    assert any(item["id"] == "sidra" for item in profiles["structuredContent"]["items"])

    with caplog.at_level(logging.INFO, logger="profile_os.mcp_server"):
        boot = _call_tool(client, "boot_profile", {"profile_id": "sidra"}).json()
    boot_data = boot["result"]["structuredContent"]
    assert boot_data["profile"]["id"] == "sidra"
    assert boot_data["base_prompt"]
    assert boot_data["role_prompt"]
    assert "mcp_tool_call name=boot_profile profile_id=sidra outcome=ok" in caplog.text

    r = _call_tool(client, "remember", {
        "profile_id": "tara",
        "kind": "note",
        "content": "MCP memory test",
        "tags": ["mcp"],
    })
    assert r.json()["result"]["isError"] is False
    hits = _call_tool(client, "search_memories", {
        "profile_id": "tara",
        "query": "MCP memory",
    }).json()["result"]["structuredContent"]["items"]
    assert len(hits) == 1

    assert _call_tool(client, "closeout", {
        "profile_id": "tara",
        "facts": "MCP state stored.",
        "texture": "Routine test.",
        "exchange": "User: done.\nAssistant: recorded.",
        "notes": "done",
    }).json()["result"]["isError"] is False

    proposed = _call_tool(client, "propose_store", {
        "profile_id": "tara",
        "name": "hotel_reservations",
        "purpose": "track hotel bookings",
        "schema": {"fields": {"hotel_name": {"type": "string"}}},
    }).json()["result"]["structuredContent"]
    assert proposed["status"] == "pending"
    assert proposed["approval_link"] == f"{PUBLIC_BASE}/approvals/store-approval-1"

    blocked = _call_tool(client, "add_record", {
        "profile_id": "tara",
        "store_name": "hotel_reservations",
        "data": {"hotel_name": "Inn"},
    }).json()["result"]
    assert blocked["isError"] is True

    bridge.store_approved = True
    added = _call_tool(client, "add_record", {
        "profile_id": "tara",
        "store_name": "hotel_reservations",
        "data": {"hotel_name": "Inn"},
    }).json()["result"]
    assert added["isError"] is False
    records = _call_tool(client, "query_records", {
        "profile_id": "tara",
        "store_name": "hotel_reservations",
        "contains": "Inn",
    }).json()["result"]["structuredContent"]["items"]
    assert records[0]["data"]["hotel_name"] == "Inn"


def test_successful_structured_content_matches_declared_output_schema():
    """Exercise representative real calls across each changed output family."""
    bridge = FakeBridge()
    client = _mcp_client(bridge)
    tools = {tool["name"]: tool for tool in MCP_TOOLS}

    def call_and_validate(name, arguments):
        result = _call_tool(client, name, arguments).json()["result"]
        assert result["isError"] is False
        Draft202012Validator(tools[name]["outputSchema"]).validate(result["structuredContent"])
        return result["structuredContent"]

    call_and_validate("list_profiles", {})
    call_and_validate("boot_profile", {"profile_id": "sidra"})
    call_and_validate("remember", {"profile_id": "tara", "kind": "note", "content": "x"})
    call_and_validate("search_memories", {"profile_id": "tara", "query": "x"})
    call_and_validate("closeout", {"profile_id": "tara", "facts": "f", "texture": "t", "exchange": "u"})
    call_and_validate("propose_store", {"profile_id": "tara", "name": "items", "purpose": "p",
                                         "schema": {"fields": {"name": {"type": "string"}}}})
    bridge.store_approved = True
    call_and_validate("add_record", {"profile_id": "tara", "store_name": "items", "data": {"name": "x"}})
    call_and_validate("query_records", {"profile_id": "tara", "store_name": "items"})


def test_session_inspector_renders_source_aware_and_raw_views():
    client = ThreadedASGIClient(create_mcp_app(bridge=FakeBridge()))

    page = client.get("/session-inspector")
    assert page.status_code == 200
    assert "Companion session inspector" in page.text
    assert "sidra" in page.text

    bad = client.post("/session-inspector", data={
        "profile_id": "sidra", "totp_code": "000000", "mode": "human",
    })
    assert bad.status_code == 401
    assert "invalid TOTP" in bad.text

    human = client.post("/session-inspector", data={
        "profile_id": "sidra", "totp_code": "123456", "mode": "human",
    })
    assert human.status_code == 200
    assert "Base prompt" in human.text
    assert "Memories" in human.text
    assert "Canonical external identity file" in human.text
    assert "lookup-only" not in human.text

    raw = client.post("/session-inspector", data={
        "profile_id": "sidra", "totp_code": "123456", "mode": "raw",
    })
    assert raw.status_code == 200
    assert "Delivered payload" in raw.text
    assert "&quot;base_prompt&quot;" in raw.text


def test_mcp_auth_origin_get_and_token_separation(tmp_path):
    backend_http = RecordingHTTPClient()
    bridge = ToolBridge(bearer=BACKEND_TOKEN, client=backend_http)
    client = _mcp_client(bridge)

    r = client.post("/mcp", json=_rpc("initialize"),
                    headers={"Origin": ORIGIN})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers["www-authenticate"]

    r = client.post("/mcp", json=_rpc("initialize"),
                    headers={**_bearer("wrong"), "Origin": ORIGIN})
    assert r.status_code == 401
    assert "invalid_token" in r.headers["www-authenticate"]

    r = client.post(
        "/mcp",
        json=_rpc("initialize"),
        headers={**_bearer(), "Origin": "https://evil.example"},
    )
    assert r.status_code == 403

    r = client.options("/mcp", headers={"Origin": ORIGIN})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == ORIGIN

    r = client.get("/mcp", headers={
        "Authorization": f"Bearer {CONNECTOR_TOKEN}",
        "Origin": ORIGIN,
        "Accept": "text/event-stream",
    })
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]

    r = _call_tool(client, "boot_profile", {"profile_id": "tara"})
    assert r.status_code == 200
    assert backend_http.requests
    seen_auth = [req["headers"].get("Authorization") for req in backend_http.requests]
    assert seen_auth[-1] == f"Bearer {BACKEND_TOKEN}"
    assert f"Bearer {CONNECTOR_TOKEN}" not in seen_auth


def test_oauth_metadata_dcr_pkce_and_bearer_use(tmp_path):
    settings = MCPSettings(
        auth_required=True,
        connector_tokens=[],
        allowed_origins=[ORIGIN],
        public_base_url=PUBLIC_BASE,
        oauth_issuer=PUBLIC_BASE,
        oauth_signing_key="oauth-test-signing-key",
        oauth_allowed_redirect_hosts=["claude.ai", "*.claude.ai"],
    )
    bridge = FakeBridge()

    async def fake_admin_verify(secret: str, totp_code: str) -> bool:
        return secret == "root-secret" and totp_code == "123456"

    client = ThreadedASGIClient(create_mcp_app(
        bridge=bridge, settings=settings, admin_verify=fake_admin_verify))

    resource = client.get("/.well-known/oauth-protected-resource").json()
    assert resource["resource"] == f"{PUBLIC_BASE}/mcp"
    assert resource["authorization_servers"] == [PUBLIC_BASE]

    authz = client.get("/.well-known/oauth-authorization-server").json()
    assert authz["registration_endpoint"] == f"{PUBLIC_BASE}/oauth/register"
    assert authz["code_challenge_methods_supported"] == ["S256"]
    # ChatGPT probes OpenID discovery after OAuth token exchange. It needs a
    # valid OIDC document, not a 404 or a bare RFC 8414 document.
    openid = client.get("/.well-known/openid-configuration").json()
    assert openid["issuer"] == authz["issuer"]
    assert openid["authorization_endpoint"] == authz["authorization_endpoint"]
    assert openid["token_endpoint"] == authz["token_endpoint"]
    assert openid["subject_types_supported"] == ["public"]
    assert openid["id_token_signing_alg_values_supported"] == ["HS256"]

    reg = client.post("/oauth/register", json={
        "client_name": "Claude",
        "redirect_uris": ["https://claude.ai/oauth/callback"],
    })
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]

    verifier = "A" * 48
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    redirect_uri = "https://claude.ai/oauth/callback"
    authorize_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-1",
        "resource": f"{PUBLIC_BASE}/mcp",
    }

    # GET renders a login form — no code is issued to an unauthenticated visitor.
    form_page = client.get("/oauth/authorize", params=authorize_params)
    assert form_page.status_code == 200
    assert "admin_secret" in form_page.text
    assert "totp_code" in form_page.text

    # Wrong secret/code: no redirect, no code.
    bad = client.post("/oauth/authorize", data={
        **authorize_params, "admin_secret": "wrong", "totp_code": "000000",
    }, follow_redirects=False)
    assert bad.status_code == 401
    assert "Invalid secret or code" in bad.text

    # Correct secret/code: issues the code and redirects.
    r = client.post("/oauth/authorize", data={
        **authorize_params, "admin_secret": "root-secret", "totp_code": "123456",
    }, follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    parsed = urllib.parse.urlparse(location)
    params = urllib.parse.parse_qs(parsed.query)
    assert params["state"] == ["state-1"]
    code = params["code"][0]

    token = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code": code,
        "code_verifier": verifier,
    })
    assert token.status_code == 200
    access_token = token.json()["access_token"]

    r = client.post(
        "/mcp",
        json=_rpc("initialize"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Origin": ORIGIN,
            "Accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 200
    assert _sse_json(r)["result"]["serverInfo"]["name"] == "profile-os-mcp"


def test_approval_link_page_totp_only_flow():
    bridge = FakeBridge()
    client = ThreadedASGIClient(create_mcp_app(bridge=bridge))

    approval = bridge.propose_prompt_edit("tara", base_prompt="New text")
    approval_id = approval["id"]

    page = client.get(f"/approvals/{approval_id}")
    assert page.status_code == 200
    assert "totp_code" in page.text
    assert "admin_secret" not in page.text  # TOTP-only, no shared secret field
    assert "New text" in page.text
    assert "base_prompt" in page.text
    assert "role_prompt" in page.text
    assert "Proposed replacement." in page.text
    assert "No change proposed — the current value remains." in page.text

    bad = client.post(f"/approvals/{approval_id}", data={
        "totp_code": "000000", "decision": "approve"})
    assert bad.status_code == 401
    assert "totp_code" in bad.text  # re-shows the form

    ok = client.post(f"/approvals/{approval_id}", data={
        "totp_code": "123456", "decision": "approve"})
    assert ok.status_code == 200
    assert "approved" in ok.text
    assert bridge.approvals[approval_id]["status"] == "approved"


def test_approval_link_rejection_needs_no_code():
    bridge = FakeBridge()
    client = ThreadedASGIClient(create_mcp_app(bridge=bridge))
    approval = bridge.propose_prompt_edit("tara", role_prompt="nope")

    r = client.post(f"/approvals/{approval['id']}", data={
        "totp_code": "", "decision": "reject"})
    assert r.status_code == 200
    assert bridge.approvals[approval["id"]]["status"] == "rejected"


def test_approval_link_unknown_id_is_404():
    bridge = FakeBridge()
    client = ThreadedASGIClient(create_mcp_app(bridge=bridge))
    r = client.get("/approvals/does-not-exist")
    assert r.status_code == 404


def test_create_profile_page_totp_only_flow():
    bridge = FakeBridge()
    client = ThreadedASGIClient(create_mcp_app(bridge=bridge))

    page = client.get("/create-profile")
    assert page.status_code == 200
    assert "totp_code" in page.text
    assert "admin_secret" not in page.text  # TOTP-only, no shared secret

    bad = client.post("/create-profile", data={
        "id": "rumbo", "display_name": "Rumbo", "totp_code": "000000"})
    assert bad.status_code == 401
    assert "totp_code" in bad.text  # re-shows the form
    assert 'value="rumbo"' in bad.text  # preserves what was typed

    ok = client.post("/create-profile", data={
        "id": "rumbo", "display_name": "Rumbo", "base_prompt": "b",
        "totp_code": "123456"})
    assert ok.status_code == 200
    assert "Created" in ok.text
    assert "rumbo" in ok.text


def test_create_profile_page_duplicate_id():
    bridge = FakeBridge()
    client = ThreadedASGIClient(create_mcp_app(bridge=bridge))
    r = client.post("/create-profile", data={
        "id": "tara", "display_name": "Tara II", "totp_code": "123456"})
    assert r.status_code == 409


def test_propose_prompt_edit_tool_returns_approval_link():
    bridge = FakeBridge()
    settings = MCPSettings(auth_required=False, public_base_url=PUBLIC_BASE)
    client = ThreadedASGIClient(create_mcp_app(bridge=bridge, settings=settings))

    r = client.post("/mcp", json=_rpc("tools/call", {
        "name": "propose_prompt_edit",
        "arguments": {"profile_id": "tara", "base_prompt": "hi"},
    }), headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 200
    result_text = _sse_json(r)["result"]["content"][0]["text"]
    assert f"{PUBLIC_BASE}/approvals/approval-1" in result_text


def test_oauth_client_registration_survives_process_restart(tmp_path):
    """A redeploy recreates the mcp container (fresh process memory). A
    connector that already completed dynamic client registration must not
    get invalid_client on its next /oauth/authorize — that's exactly what
    broke ChatGPT's connection after a mid-session redeploy."""
    state_file = str(tmp_path / "oauth-state" / "clients.json")

    first_process_state = OAuthState(state_file=state_file)
    client = first_process_state.register(
        ["https://chatgpt.com/connector/oauth/abc"], "ChatGPT")

    # Simulate the container being recreated: a brand-new OAuthState loading
    # from the same (persisted) file, with nothing carried over in memory.
    second_process_state = OAuthState(state_file=state_file)
    reloaded = second_process_state.get_client(client.client_id)
    assert reloaded is not None
    assert reloaded.redirect_uris == ["https://chatgpt.com/connector/oauth/abc"]
    assert reloaded.client_name == "ChatGPT"


def test_oauth_state_without_file_does_not_persist(tmp_path):
    """No state_file configured (e.g. local dev) — in-memory only, same as
    before; must not raise."""
    state = OAuthState(state_file=None)
    client = state.register(["https://claude.ai/oauth/callback"], "Claude")
    assert state.get_client(client.client_id) is not None


def test_bootstrap_bridge_cli_grants_operational_only(tmp_path):
    data_dir = str(tmp_path / "data")
    result = bootstrap(data_dir, "bridge-secret", profiles=["tara"])
    assert result["operations"] == BRIDGE_OPS

    store = Store(data_dir)
    try:
        access = AccessControl(store)
        principal_id = access.authenticate_secret("bridge-secret")
        assert principal_id is not None
        assert access.allowed(principal_id, "boot", "tara")
        assert access.allowed(principal_id, "records:write", "tara")
        assert access.allowed(principal_id, "stores:propose", "tara")
        assert not access.allowed(principal_id, "audit:read", "tara")
        assert not access.allowed(principal_id, "stores:approve", "tara")
        assert not access.allowed(principal_id, "boot", "sidra")
    finally:
        store.close()
