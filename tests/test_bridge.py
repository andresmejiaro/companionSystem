import pytest
from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.bridge import TOOLS, ToolBridge, ToolBridgeError

SCHEMA = {"fields": {"hotel_name": {"type": "string"}}}

OPERATIONAL_GRANTS = ["boot", "remember", "search", "closeout",
                      "stores:propose", "records:read", "records:write",
                      "audit:read"]


def _bridge_over(app, bearer=None):
    return ToolBridge(bearer=bearer, client=TestClient(app))


def _grant_bridge(access, secret, profiles):
    p = access.create_principal("bridge", "test bridge")
    access.create_credential(p["id"], "k", secret)
    for profile in profiles:
        for op in OPERATIONAL_GRANTS:
            access.grant(p["id"], op, profile_id=profile)
    return p


def _exercise_all_tools(bridge, profile="tara"):
    """Every exposed tool round-trips through the real API."""
    assert bridge.boot(profile)["profile"]["id"] == profile
    ev = bridge.remember(profile, "note", "bridged memory", tags=["t"])
    assert ev["content"] == "bridged memory"
    assert any("bridged" in e["content"]
               for e in bridge.search_memories(profile, "bridged"))
    bridge.closeout(profile, "notes", "State after bridge demo.")
    prop = bridge.propose_store(profile, "hotel_reservations", "p", SCHEMA)
    assert prop["status"] == "pending"
    assert any(s["name"] == "hotel_reservations"
               for s in bridge.list_stores(profile))
    assert bridge.get_store(profile, "hotel_reservations")["status"] == "pending"
    # approval is admin-only and NOT a bridge tool: use the service layer
    bridge._client.app.state.dynstores.approve(profile, "hotel_reservations")
    rec = bridge.add_record(profile, "hotel_reservations",
                            {"hotel_name": "Inn"})
    assert rec["data"]["hotel_name"] == "Inn"
    assert bridge.query_records(profile, "hotel_reservations",
                                contains="Inn")
    assert bridge.audit(profile)
    assert bridge.audit(profile, "hotel_reservations")


def test_bridge_against_auth_disabled_app(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"))
    _exercise_all_tools(_bridge_over(app))


def test_bridge_against_auth_enabled_app_with_grants(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    _grant_bridge(app.state.access, "bridge-secret", ["tara"])
    _exercise_all_tools(_bridge_over(app, bearer="bridge-secret"))


def test_bridge_sends_bearer_header(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"))
    seen = {}

    @app.middleware("http")
    async def capture(request, call_next):
        seen["auth"] = request.headers.get("authorization")
        return await call_next(request)

    _bridge_over(app, bearer="s3cret").boot("tara")
    assert seen["auth"] == "Bearer s3cret"
    _bridge_over(app).boot("tara")  # no bearer configured → no header
    assert seen["auth"] is None


def test_bridge_gets_401_and_403_from_backend(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    with pytest.raises(ToolBridgeError) as e:
        _bridge_over(app).boot("tara")  # no credential
    assert e.value.status_code == 401
    with pytest.raises(ToolBridgeError) as e:
        _bridge_over(app, bearer="wrong").boot("tara")
    assert e.value.status_code == 401

    access = app.state.access
    p = access.create_principal("bridge", "narrow")
    access.create_credential(p["id"], "k", "narrow-secret")
    access.grant(p["id"], "boot", profile_id="tara")
    bridge = _bridge_over(app, bearer="narrow-secret")
    bridge.boot("tara")  # granted
    with pytest.raises(ToolBridgeError) as e:
        bridge.remember("tara", "note", "x")  # ungranted operation
    assert e.value.status_code == 403
    with pytest.raises(ToolBridgeError) as e:
        bridge.boot("sidra")  # ungranted profile
    assert e.value.status_code == 403


def test_no_tool_bypasses_api_authorization(tmp_path):
    """Every exposed tool hits the enforced API: all 401 without credential."""
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    bridge = _bridge_over(app)
    calls = {
        "boot": {"profile_id": "tara"},
        "remember": {"profile_id": "tara", "kind": "note", "content": "x"},
        "search_memories": {"profile_id": "tara", "query": "x"},
        "closeout": {"profile_id": "tara", "notes": "", "new_state": "s"},
        "propose_store": {"profile_id": "tara", "name": "n1", "purpose": "p",
                          "schema": SCHEMA},
        "list_stores": {"profile_id": "tara"},
        "get_store": {"profile_id": "tara", "name": "n1"},
        "add_record": {"profile_id": "tara", "store_name": "n1", "data": {}},
        "query_records": {"profile_id": "tara", "store_name": "n1"},
        "audit": {"profile_id": "tara"},
    }
    assert set(calls) == {t["name"] for t in TOOLS}
    for name, args in calls.items():
        with pytest.raises(ToolBridgeError) as e:
            bridge.call(name, args)
        assert e.value.status_code == 401, name


def test_call_dispatch_and_unknown_tool(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"))
    bridge = _bridge_over(app)
    assert bridge.call("boot", {"profile_id": "tara"})["profile"]["id"] == "tara"
    with pytest.raises(ToolBridgeError) as e:
        bridge.call("approve_store", {"profile_id": "tara"})
    assert e.value.status_code == 404


def test_env_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFILE_OS_BRIDGE_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PROFILE_OS_BRIDGE_BEARER", "env-secret")
    bridge = ToolBridge()
    assert bridge._base_url == "http://127.0.0.1:9"
    assert bridge._bearer == "env-secret"


def test_tool_specs_are_mcp_shaped():
    for t in TOOLS:
        assert set(t) == {"name", "description", "inputSchema"}
        assert t["inputSchema"]["type"] == "object"
        assert set(t["inputSchema"]["required"]) <= set(
            t["inputSchema"]["properties"])
    names = {t["name"] for t in TOOLS}
    assert not names & {"approve_store", "reject_store", "archive_store"}
