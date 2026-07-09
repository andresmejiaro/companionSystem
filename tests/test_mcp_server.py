import base64
import concurrent.futures
import hashlib
import logging
import urllib.parse

import anyio
import httpx

from profile_os.bootstrap_bridge import BRIDGE_OPS, bootstrap
from profile_os.access import AccessControl
from profile_os.bridge import ToolBridge, ToolBridgeError
from profile_os.mcp_server import MCPSettings, MCP_TOOLS, create_mcp_app
from profile_os.storage import Store


CONNECTOR_TOKEN = "claude-connector-token"
BACKEND_TOKEN = "profile-os-backend-token"
ORIGIN = "https://claude.ai"
PUBLIC_BASE = "https://profiles.example"


class ThreadedASGIClient:
    """Tiny sync client for tests that avoids Starlette TestClient.

    The current local venv has a FastAPI/Starlette TestClient compatibility
    issue on Python 3.14. This client still exercises the ASGI app through
    httpx.ASGITransport, and the thread boundary lets the sync ToolBridge call
    the backend while the MCP app is already running in an event loop.
    """

    def __init__(self, app, base_url: str = "http://testserver"):
        self.app = app
        self.base_url = base_url

    def request(self, method: str, url: str, **kwargs):
        def run_request():
            async def send():
                transport = httpx.ASGITransport(app=self.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url=self.base_url,
                ) as client:
                    response = await client.request(method, url, **kwargs)
                    await response.aread()
                    return response

            return anyio.run(send)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(run_request).result()

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def options(self, url: str, **kwargs):
        return self.request("OPTIONS", url, **kwargs)

    def close(self):
        return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _bearer(token: str = CONNECTOR_TOKEN) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Origin": ORIGIN,
        "Accept": "application/json, text/event-stream",
    }


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

    def list_profiles(self):
        return [
            {"id": "sidra", "display_name": "Sidra"},
            {"id": "tara", "display_name": "Tara"},
        ]

    def boot_profile(self, profile_id: str):
        return {
            "profile": {
                "id": profile_id,
                "display_name": profile_id.title(),
                "allowed_tools": ["remember", "search_memories"],
                "memory_policy": {"max_boot_events": 10},
                "closeout_rules": "Write compact state.",
            },
            "base_prompt": "Base prompt.",
            "role_prompt": "Role prompt.",
            "compact_state": "No active task.",
            "recent_memories": list(self.memories),
        }

    def remember(self, profile_id, kind, content, tags=None):
        event = {
            "id": "mem-1",
            "profile_id": profile_id,
            "kind": kind,
            "content": content,
            "tags": tags or [],
        }
        self.memories.append(event)
        return event

    def search_memories(self, profile_id, query, limit=20):
        return [m for m in self.memories if query.lower() in m["content"].lower()][:limit]

    def closeout(self, profile_id, notes, new_state):
        return {"id": "closeout-1", "profile_id": profile_id,
                "notes": notes, "new_state": new_state}

    def list_stores(self, profile_id):
        return list(self.stores.values())

    def propose_store(self, profile_id, name, purpose, schema):
        store = {"name": name, "purpose": purpose, "schema": schema,
                 "status": "pending", "profile_id": profile_id}
        self.stores[name] = store
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
        record = {"id": "rec-1", "store": store_name, "data": data}
        self.records.append(record)
        return record


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


def test_initialize_and_list_tools(tmp_path):
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
    names = {tool["name"] for tool in r.json()["result"]["tools"]}
    assert names == {
        "whoami",
        "start_session",
        "propose_prompt_edit",
        "list_profiles",
        "boot_profile",
        "remember",
        "search_memories",
        "closeout",
        "list_stores",
        "propose_store",
        "query_records",
        "add_record",
    }
    assert not names & {"approve_store", "reject_store", "archive_store", "audit"}
    assert names == {tool["name"] for tool in MCP_TOOLS}


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
        "notes": "done",
        "new_state": "MCP state stored.",
    }).json()["result"]["isError"] is False

    proposed = _call_tool(client, "propose_store", {
        "profile_id": "tara",
        "name": "hotel_reservations",
        "purpose": "track hotel bookings",
        "schema": {"fields": {"hotel_name": {"type": "string"}}},
    }).json()["result"]["structuredContent"]
    assert proposed["status"] == "pending"

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
    client = ThreadedASGIClient(create_mcp_app(bridge=bridge, settings=settings))

    resource = client.get("/.well-known/oauth-protected-resource").json()
    assert resource["resource"] == f"{PUBLIC_BASE}/mcp"
    assert resource["authorization_servers"] == [PUBLIC_BASE]

    authz = client.get("/.well-known/oauth-authorization-server").json()
    assert authz["registration_endpoint"] == f"{PUBLIC_BASE}/oauth/register"
    assert authz["code_challenge_methods_supported"] == ["S256"]

    reg = client.post("/oauth/register", json={
        "client_name": "Claude",
        "redirect_uris": ["https://claude.ai/oauth/callback"],
    })
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]

    verifier = "A" * 48
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    redirect_uri = "https://claude.ai/oauth/callback"
    r = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-1",
        "resource": f"{PUBLIC_BASE}/mcp",
    }, follow_redirects=False)
    assert r.status_code in {302, 307}
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
    assert r.json()["result"]["serverInfo"]["name"] == "profile-os-mcp"


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
