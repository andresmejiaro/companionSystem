import time

import pytest
from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.bootstrap_admin import bootstrap

SCHEMA = {"fields": {"hotel_name": {"type": "string"}}}


def _propose(client, profile="tara", name="hotel_reservations"):
    r = client.post(f"/profiles/{profile}/stores",
                    json={"name": name, "purpose": "p", "proposed_by": profile,
                          "schema": SCHEMA})
    assert r.status_code == 201


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def _propose_direct(client, profile="tara", name="hotel_reservations"):
    """Seed a proposal via the service layer (routes now require a grant)."""
    client.app.state.dynstores.propose(profile, name, "p", profile, SCHEMA)


@pytest.fixture
def auth_client(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    with TestClient(app) as c:
        yield c, app.state.access


def test_auth_disabled_lifecycle_open_by_default(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"))  # env unset in tests
    with TestClient(app) as client:
        _propose(client)
        r = client.post("/profiles/tara/stores/hotel_reservations/approve")
        assert r.status_code == 200
        r = client.post("/profiles/tara/stores/hotel_reservations/archive")
        assert r.status_code == 200


def test_auth_enabled_requires_credential(auth_client):
    client, _ = auth_client
    _propose_direct(client)
    assert client.post("/profiles/tara/stores/hotel_reservations/approve").status_code == 401
    r = client.post("/profiles/tara/stores/hotel_reservations/approve",
                    headers=_bearer("wrong-secret"))
    assert r.status_code == 401
    r = client.post("/profiles/tara/stores/hotel_reservations/reject",
                    json={"reason": "x"})
    assert r.status_code == 401
    assert client.post("/profiles/tara/stores/hotel_reservations/archive").status_code == 401


def test_valid_credential_without_grant_is_403(auth_client):
    client, access = auth_client
    p = access.create_principal("app", "no-grant app")
    access.create_credential(p["id"], "k", "app-secret")
    _propose_direct(client)
    r = client.post("/profiles/tara/stores/hotel_reservations/approve",
                    headers=_bearer("app-secret"))
    assert r.status_code == 403


def test_profile_scoped_grant(auth_client):
    client, access = auth_client
    p = access.create_principal("human", "tara approver")
    access.create_credential(p["id"], "k", "tara-secret")
    access.grant(p["id"], "stores:approve", profile_id="tara")
    _propose_direct(client, "tara")
    _propose_direct(client, "sidra", "notes_store")
    r = client.post("/profiles/tara/stores/hotel_reservations/approve",
                    headers=_bearer("tara-secret"))
    assert r.status_code == 200
    # tara grant must not authorize sidra
    r = client.post("/profiles/sidra/stores/notes_store/approve",
                    headers=_bearer("tara-secret"))
    assert r.status_code == 403


def test_wildcard_grant_spans_profiles(auth_client):
    client, access = auth_client
    p = access.create_principal("admin", "operator")
    access.create_credential(p["id"], "k", "admin-secret")
    access.grant(p["id"], "stores:approve", profile_id="*")
    _propose_direct(client, "tara")
    _propose_direct(client, "sidra", "notes_store")
    assert client.post("/profiles/tara/stores/hotel_reservations/approve",
                       headers=_bearer("admin-secret")).status_code == 200
    assert client.post("/profiles/sidra/stores/notes_store/approve",
                       headers=_bearer("admin-secret")).status_code == 200


def test_revoked_expired_disabled_are_401(auth_client):
    client, access = auth_client
    _propose_direct(client)
    url = "/profiles/tara/stores/hotel_reservations/approve"

    p1 = access.create_principal("app", "revoked cred")
    c1 = access.create_credential(p1["id"], "k", "revoked-secret")
    access.grant(p1["id"], "stores:approve", profile_id="tara")
    access.revoke_credential(c1["id"])
    assert client.post(url, headers=_bearer("revoked-secret")).status_code == 401

    p2 = access.create_principal("app", "expired cred")
    access.create_credential(p2["id"], "k", "expired-secret",
                             expires_at=time.time() - 1)
    access.grant(p2["id"], "stores:approve", profile_id="tara")
    assert client.post(url, headers=_bearer("expired-secret")).status_code == 401

    p3 = access.create_principal("app", "disabled principal")
    access.create_credential(p3["id"], "k", "disabled-secret")
    access.grant(p3["id"], "stores:approve", profile_id="tara")
    access.disable_principal(p3["id"])
    assert client.post(url, headers=_bearer("disabled-secret")).status_code == 401


def test_health_and_demo_remain_public(auth_client):
    client, _ = auth_client
    assert client.get("/health").status_code == 200
    assert client.get("/demo").status_code == 200


def test_all_protected_endpoints_401_without_credential(auth_client):
    client, _ = auth_client
    protected = [
        ("GET", "/profiles", None),
        ("GET", "/profiles/tara", None),
        ("POST", "/profiles/tara/boot", None),
        ("POST", "/profiles/tara/memories", {"kind": "note", "content": "x"}),
        ("GET", "/profiles/tara/memories/search?q=x", None),
        ("POST", "/profiles/tara/closeout", {"notes": "", "new_state": "s"}),
        ("GET", "/profiles/tara/domain", None),
        ("GET", "/profiles/tara/domain/notes", None),
        ("POST", "/profiles/tara/domain/notes", {"data": {}}),
        ("POST", "/profiles/tara/stores",
         {"name": "n", "purpose": "p", "proposed_by": "t", "schema": SCHEMA}),
        ("GET", "/profiles/tara/stores", None),
        ("GET", "/profiles/tara/stores/n", None),
        ("POST", "/profiles/tara/stores/n/records", {"data": {}}),
        ("GET", "/profiles/tara/stores/n/records", None),
        ("GET", "/profiles/tara/stores/n/audit", None),
        ("GET", "/profiles/tara/audit", None),
    ]
    for method, url, body in protected:
        r = client.request(method, url, json=body)
        assert r.status_code == 401, f"{method} {url} -> {r.status_code}"
        r = client.request(method, url, json=body, headers=_bearer("wrong"))
        assert r.status_code == 401, f"{method} {url} bad key -> {r.status_code}"


def test_each_operation_grant_gates_its_endpoint(auth_client):
    client, access = auth_client
    p = access.create_principal("app", "tara app")
    access.create_credential(p["id"], "k", "tara-app-secret")
    h = _bearer("tara-app-secret")

    # ungranted operations are 403
    assert client.post("/profiles/tara/boot", headers=h).status_code == 403
    assert client.get("/profiles/tara/audit", headers=h).status_code == 403

    access.grant(p["id"], "boot", profile_id="tara")
    assert client.post("/profiles/tara/boot", headers=h).status_code == 200
    assert client.get("/profiles/tara", headers=h).status_code == 200
    assert client.post("/profiles/tara/memories", headers=h,
                       json={"kind": "note", "content": "x"}).status_code == 403

    access.grant(p["id"], "remember", profile_id="tara")
    assert client.post("/profiles/tara/memories", headers=h,
                       json={"kind": "note", "content": "x"}).status_code == 201
    assert client.get("/profiles/tara/memories/search?q=x",
                      headers=h).status_code == 403

    access.grant(p["id"], "search", profile_id="tara")
    assert client.get("/profiles/tara/memories/search?q=x",
                      headers=h).status_code == 200

    access.grant(p["id"], "closeout", profile_id="tara")
    assert client.post("/profiles/tara/closeout", headers=h,
                       json={"notes": "", "new_state": "resting"}).status_code == 201

    assert client.post("/profiles/tara/stores", headers=h,
                       json={"name": "hotel_reservations", "purpose": "p",
                             "proposed_by": "tara", "schema": SCHEMA}).status_code == 403
    access.grant(p["id"], "stores:propose", profile_id="tara")
    assert client.post("/profiles/tara/stores", headers=h,
                       json={"name": "hotel_reservations", "purpose": "p",
                             "proposed_by": "tara", "schema": SCHEMA}).status_code == 201

    assert client.get("/profiles/tara/stores", headers=h).status_code == 403
    access.grant(p["id"], "records:read", profile_id="tara")
    assert client.get("/profiles/tara/stores", headers=h).status_code == 200
    assert client.get("/profiles/tara/stores/hotel_reservations",
                      headers=h).status_code == 200
    assert client.get("/profiles/tara/domain", headers=h).status_code == 200

    assert client.post("/profiles/tara/stores/hotel_reservations/approve",
                       headers=h).status_code == 403
    access.grant(p["id"], "stores:approve", profile_id="tara")
    assert client.post("/profiles/tara/stores/hotel_reservations/approve",
                       headers=h).status_code == 200

    assert client.post("/profiles/tara/stores/hotel_reservations/records",
                       headers=h,
                       json={"data": {"hotel_name": "Inn"}}).status_code == 403
    access.grant(p["id"], "records:write", profile_id="tara")
    assert client.post("/profiles/tara/stores/hotel_reservations/records",
                       headers=h,
                       json={"data": {"hotel_name": "Inn"}}).status_code == 201
    assert client.get("/profiles/tara/stores/hotel_reservations/records",
                      headers=h).status_code == 200

    assert client.get("/profiles/tara/audit", headers=h).status_code == 403
    access.grant(p["id"], "audit:read", profile_id="tara")
    assert client.get("/profiles/tara/audit", headers=h).status_code == 200
    assert client.get("/profiles/tara/stores/hotel_reservations/audit",
                      headers=h).status_code == 200

    # tara grants never authorize sidra
    assert client.post("/profiles/sidra/boot", headers=h).status_code == 403
    assert client.get("/profiles/sidra/audit", headers=h).status_code == 403


def test_list_profiles_filtered_by_grants(auth_client):
    client, access = auth_client
    p = access.create_principal("app", "tara only")
    access.create_credential(p["id"], "k", "tara-secret")
    access.grant(p["id"], "boot", profile_id="tara")
    r = client.get("/profiles", headers=_bearer("tara-secret"))
    assert r.status_code == 200
    assert [prof["id"] for prof in r.json()] == ["tara"]

    w = access.create_principal("admin", "operator")
    access.create_credential(w["id"], "k", "admin-secret")
    access.grant(w["id"], "boot", profile_id="*")
    r = client.get("/profiles", headers=_bearer("admin-secret"))
    assert r.status_code == 200
    assert {prof["id"] for prof in r.json()} >= {"tara", "sidra"}

    n = access.create_principal("app", "no grants")
    access.create_credential(n["id"], "k", "none-secret")
    r = client.get("/profiles", headers=_bearer("none-secret"))
    assert r.status_code == 200 and r.json() == []


def test_wildcard_grant_accesses_all_profiles(auth_client):
    client, access = auth_client
    p = access.create_principal("admin", "operator")
    access.create_credential(p["id"], "k", "admin-secret")
    for op in ("boot", "stores:propose", "stores:approve"):
        access.grant(p["id"], op, profile_id="*")
    h = _bearer("admin-secret")
    for profile in ("tara", "sidra"):
        assert client.post(f"/profiles/{profile}/boot", headers=h).status_code == 200
        r = client.post(f"/profiles/{profile}/stores", headers=h,
                        json={"name": "wild_store", "purpose": "p", "proposed_by": profile,
                              "schema": SCHEMA})
        assert r.status_code == 201
        assert client.post(f"/profiles/{profile}/stores/wild_store/approve",
                           headers=h).status_code == 200


def test_identity_route(tmp_path, monkeypatch):
    identity_path = tmp_path / "quien_soy.md"
    identity_path.write_text("canonical facts")
    monkeypatch.setenv("PROFILE_OS_IDENTITY_FILE", str(identity_path))
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    with TestClient(app) as client:
        access = app.state.access
        assert client.get("/identity").status_code == 401

        p = access.create_principal("app", "no-grant")
        access.create_credential(p["id"], "k", "no-grant-secret")
        r = client.get("/identity", headers=_bearer("no-grant-secret"))
        assert r.status_code == 403

        g = access.create_principal("app", "identity reader")
        access.create_credential(g["id"], "k", "identity-secret")
        access.grant(g["id"], "identity:read", profile_id=None)
        r = client.get("/identity", headers=_bearer("identity-secret"))
        assert r.status_code == 200
        assert r.json() == {"content": "canonical facts"}


def test_identity_route_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PROFILE_OS_IDENTITY_FILE", raising=False)
    app = create_app(data_dir=str(tmp_path / "data"))  # auth disabled
    with TestClient(app) as client:
        assert client.get("/identity").status_code == 404


def test_bootstrap_admin_cli(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    result = bootstrap(data_dir, "root-secret")
    assert result["granted"] == ["stores:approve", "audit:read",
                                 "manage_grants", "credentials:manage"]
    # idempotent: same principal, no duplicate grants, new credential ok
    again = bootstrap(data_dir, "another-secret")
    assert again["principal_id"] == result["principal_id"]
    assert again["granted"] == []

    # the credential actually works against an auth-enabled app on same data
    app = create_app(data_dir=data_dir, auth_enabled=True)
    with TestClient(app) as client:
        _propose_direct(client)
        assert client.post("/profiles/tara/stores/hotel_reservations/approve",
                           headers=_bearer("root-secret")).status_code == 200

    # CLI output never contains the secret
    from profile_os.bootstrap_admin import main
    main(["--data-dir", data_dir, "--secret", "cli-secret"])
    out = capsys.readouterr().out
    assert "cli-secret" not in out and "principal" in out
