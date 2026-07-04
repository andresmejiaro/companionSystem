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
    _propose(client)
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
    _propose(client)
    r = client.post("/profiles/tara/stores/hotel_reservations/approve",
                    headers=_bearer("app-secret"))
    assert r.status_code == 403


def test_profile_scoped_grant(auth_client):
    client, access = auth_client
    p = access.create_principal("human", "tara approver")
    access.create_credential(p["id"], "k", "tara-secret")
    access.grant(p["id"], "stores:approve", profile_id="tara")
    _propose(client, "tara")
    _propose(client, "sidra", "notes_store")
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
    _propose(client, "tara")
    _propose(client, "sidra", "notes_store")
    assert client.post("/profiles/tara/stores/hotel_reservations/approve",
                       headers=_bearer("admin-secret")).status_code == 200
    assert client.post("/profiles/sidra/stores/notes_store/approve",
                       headers=_bearer("admin-secret")).status_code == 200


def test_revoked_expired_disabled_are_401(auth_client):
    client, access = auth_client
    _propose(client)
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


def test_non_lifecycle_endpoints_not_newly_protected(auth_client):
    client, _ = auth_client
    assert client.post("/profiles/tara/boot").status_code == 200
    assert client.get("/profiles").status_code == 200
    assert client.post("/profiles/tara/memories",
                       json={"kind": "note", "content": "open"}).status_code == 201
    _propose(client)  # proposing needs no credential in this slice
    assert client.get("/demo").status_code == 200


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
        _propose(client)
        assert client.post("/profiles/tara/stores/hotel_reservations/approve",
                           headers=_bearer("root-secret")).status_code == 200

    # CLI output never contains the secret
    from profile_os.bootstrap_admin import main
    main(["--data-dir", data_dir, "--secret", "cli-secret"])
    out = capsys.readouterr().out
    assert "cli-secret" not in out and "principal" in out
