import pytest
from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.bridge import TOOLS, ToolBridge
from profile_os.harness import HARNESS_TOOLS, HarnessError, run_harness

OPERATIONAL_GRANTS = ["boot", "remember", "search", "closeout",
                      "stores:propose", "records:read", "records:write",
                      "audit:read"]


def _bridge_over(app, bearer=None):
    return ToolBridge(bearer=bearer, client=TestClient(app))


def _grant_bridge(access, secret, profile="tara"):
    p = access.create_principal("bridge", "harness bridge")
    access.create_credential(p["id"], "k", secret)
    for op in OPERATIONAL_GRANTS:
        access.grant(p["id"], op, profile_id=profile)


def test_harness_against_auth_disabled_app(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"))
    report = run_harness(_bridge_over(app), printer=lambda *_: None)
    assert "Harness OK" in report["summary"]
    # the expected 409 happened and the store was left pending
    assert any(c["status"] == 409 and c.get("expected")
               for c in report["calls"])
    store = app.state.dynstores.get(  # still pending: nothing approved it
        "tara", report["store_name"])
    assert store["status"] == "pending"


def test_harness_against_auth_enabled_app_with_grants(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    _grant_bridge(app.state.access, "harness-secret")
    report = run_harness(_bridge_over(app, bearer="harness-secret"),
                         printer=lambda *_: None)
    assert "Harness OK" in report["summary"]


def test_harness_fails_clearly_without_credential(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    with pytest.raises(HarnessError) as e:
        run_harness(_bridge_over(app), printer=lambda *_: None)
    assert e.value.cause.status_code == 401
    assert "PROFILE_OS_BRIDGE_BEARER" in str(e.value)


def test_harness_fails_clearly_with_bad_credential(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    with pytest.raises(HarnessError) as e:
        run_harness(_bridge_over(app, bearer="wrong"),
                    printer=lambda *_: None)
    assert e.value.cause.status_code == 401


def test_harness_fails_clearly_with_missing_grant(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    access = app.state.access
    p = access.create_principal("bridge", "boot only")
    access.create_credential(p["id"], "k", "narrow")
    access.grant(p["id"], "boot", profile_id="tara")
    with pytest.raises(HarnessError) as e:
        run_harness(_bridge_over(app, bearer="narrow"),
                    printer=lambda *_: None)
    assert e.value.cause.status_code == 403
    assert "grant" in str(e.value)


def test_harness_uses_no_lifecycle_tools(tmp_path):
    """Only bridge tools, never approve/reject/archive."""
    bridge_tools = {t["name"] for t in TOOLS}
    assert set(HARNESS_TOOLS) <= bridge_tools
    forbidden = {"approve_store", "reject_store", "archive_store",
                 "approve", "reject", "archive"}
    assert not set(HARNESS_TOOLS) & forbidden

    app = create_app(data_dir=str(tmp_path / "data"))
    used = []
    bridge = _bridge_over(app)
    orig = bridge.call
    bridge.call = lambda name, args: (used.append(name), orig(name, args))[1]
    run_harness(bridge, printer=lambda *_: None)
    assert set(used) <= set(HARNESS_TOOLS)


def test_harness_cli_entrypoint_wiring():
    from profile_os import harness
    assert callable(harness.main)
