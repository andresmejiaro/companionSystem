import base64
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.enroll import Enrollment
from profile_os.mint_invite import main as mint_invite_main
from profile_os.sign import sign_request


def _pubkey_b64(private_key: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


@pytest.fixture
def auth_client(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    with TestClient(app) as c:
        yield c, app.state.access, app.state.enrollment


def _enroll(client, enrollment, display_name="rumbo-agent"):
    invite_id, token = enrollment.mint_invite(display_name_hint=display_name)
    private_key = Ed25519PrivateKey.generate()
    r = client.post("/enroll", json={
        "invite_token": token, "display_name": display_name,
        "public_key": _pubkey_b64(private_key)})
    assert r.status_code == 201, r.text
    return r.json(), private_key


def _signed(client, private_key, key_id, method, path, body: dict | None = None):
    """Sign against the path component only (no query string), matching
    what the server reads from request.url.path."""
    sign_path = path.split("?", 1)[0]
    body_bytes = b"" if body is None else __import__("json").dumps(body).encode()
    header = sign_request(private_key, key_id, method, sign_path, body_bytes)
    kwargs = {"headers": {"Authorization": header}}
    if body is not None:
        kwargs["content"] = body_bytes
        kwargs["headers"]["Content-Type"] = "application/json"
    return client.request(method, path, **kwargs)


def test_enroll_happy_path_and_signed_profile_creation(auth_client):
    client, access, enrollment = auth_client
    enrolled, private_key = _enroll(client, enrollment)
    principal_id, key_id = enrolled["principal_id"], enrolled["key_id"]

    r = _signed(client, private_key, key_id, "POST", "/profiles",
               body={"id": "rumbo", "display_name": "Rumbo",
                     "base_prompt": "b", "role_prompt": "r"})
    assert r.status_code == 201, r.text

    # zero further grants needed: boot / remember / search / closeout all work
    for method, path, body in [
        ("POST", "/profiles/rumbo/boot", None),
        ("POST", "/profiles/rumbo/memories", {"kind": "note", "content": "hi"}),
        ("GET", "/profiles/rumbo/memories/search?q=hi", None),
        ("POST", "/profiles/rumbo/closeout", {"facts": "s", "texture": "t", "exchange": "User: s."}),
    ]:
        r = _signed(client, private_key, key_id, method, path, body)
        assert r.status_code in (200, 201), f"{method} {path} -> {r.status_code}: {r.text}"

    # cannot approve stores (admin-only) and cannot touch another profile
    r = _signed(client, private_key, key_id, "POST",
               "/profiles/rumbo/stores/whatever/approve")
    assert r.status_code in (403, 404)
    r = _signed(client, private_key, key_id, "POST", "/profiles/tara/boot")
    assert r.status_code == 403

    # cannot grant itself anything or create a second principal
    assert not access.allowed(principal_id, "manage_grants", "rumbo")
    assert not access.allowed(principal_id, "credentials:manage", None)


def test_store_auto_approval_budget(auth_client):
    client, access, enrollment = auth_client
    enrolled, private_key = _enroll(client, enrollment)
    key_id = enrolled["key_id"]
    _signed(client, private_key, key_id, "POST", "/profiles",
           body={"id": "rumbo", "display_name": "Rumbo"})

    schema_small = {"fields": {"aa": {"type": "string"}}}
    for i in range(3):
        r = _signed(client, private_key, key_id, "POST", "/profiles/rumbo/stores",
                   body={"name": f"store{i}", "purpose": "p",
                         "proposed_by": "rumbo", "schema": schema_small})
        assert r.status_code == 201
        assert r.json()["status"] == "approved", r.json()
        # auto-approved stores accept writes immediately
        r = _signed(client, private_key, key_id, "POST",
                   f"/profiles/rumbo/stores/store{i}/records", body={"data": {"aa": "x"}})
        assert r.status_code == 201

    # 4th store goes to pending (over the default budget of 3)
    r = _signed(client, private_key, key_id, "POST", "/profiles/rumbo/stores",
               body={"name": "store4", "purpose": "p", "proposed_by": "rumbo",
                     "schema": schema_small})
    assert r.status_code == 201
    assert r.json()["status"] == "pending"

    # an oversized schema (>12 fields) also goes to pending
    big_schema = {"fields": {f"f{i}": {"type": "string"} for i in range(13)}}
    r = _signed(client, private_key, key_id, "POST", "/profiles/rumbo/stores",
               body={"name": "bigstore", "purpose": "p", "proposed_by": "rumbo",
                     "schema": big_schema})
    assert r.status_code == 201
    assert r.json()["status"] == "pending"


def test_reused_invite_is_410(auth_client):
    client, access, enrollment = auth_client
    invite_id, token = enrollment.mint_invite()
    private_key = Ed25519PrivateKey.generate()
    body = {"invite_token": token, "display_name": "agent-a",
           "public_key": _pubkey_b64(private_key)}
    assert client.post("/enroll", json=body).status_code == 201
    body2 = {**body, "public_key": _pubkey_b64(Ed25519PrivateKey.generate())}
    assert client.post("/enroll", json=body2).status_code == 410


def test_expired_invite_is_410(auth_client):
    client, access, enrollment = auth_client
    invite_id, token = enrollment.mint_invite(expires_hours=-1)
    private_key = Ed25519PrivateKey.generate()
    r = client.post("/enroll", json={
        "invite_token": token, "display_name": "agent-a",
        "public_key": _pubkey_b64(private_key)})
    assert r.status_code == 410


def test_bad_invite_token_is_401(auth_client):
    client, access, enrollment = auth_client
    r = client.post("/enroll", json={
        "invite_token": "not-a-real-token", "display_name": "agent-a",
        "public_key": _pubkey_b64(Ed25519PrivateKey.generate())})
    assert r.status_code == 401


def test_signature_tamper_replay_expiry_revoked(auth_client):
    client, access, enrollment = auth_client
    enrolled, private_key = _enroll(client, enrollment)
    key_id = enrolled["key_id"]

    # tampered path -> 401
    header = sign_request(private_key, key_id, "GET", "/profiles/rumbo", b"")
    r = client.get("/profiles/other", headers={"Authorization": header})
    assert r.status_code == 401

    # tampered body -> 401
    header = sign_request(private_key, key_id, "POST", "/profiles",
                          b'{"id":"a"}')
    r = client.post("/profiles", content=b'{"id":"b","display_name":"B"}',
                    headers={"Authorization": header,
                            "Content-Type": "application/json"})
    assert r.status_code == 401

    # replayed nonce -> 401 on second use
    header = sign_request(private_key, key_id, "GET", "/profiles", b"")
    r1 = client.get("/profiles", headers={"Authorization": header})
    assert r1.status_code == 200
    r2 = client.get("/profiles", headers={"Authorization": header})
    assert r2.status_code == 401

    # expired timestamp -> 401
    import re
    fresh = sign_request(private_key, key_id, "GET", "/profiles", b"")
    stale = re.sub(r"ts=\d+", f"ts={int(time.time()) - 1000}", fresh)
    r = client.get("/profiles", headers={"Authorization": stale})
    assert r.status_code == 401

    # revoked key -> 401
    row = access.db.execute("SELECT id FROM access_credentials WHERE id=?",
                            (key_id,)).fetchone()
    access.revoke_credential(row["id"])
    header = sign_request(private_key, key_id, "GET", "/profiles", b"")
    r = client.get("/profiles", headers={"Authorization": header})
    assert r.status_code == 401


def test_mint_invite_cli(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    mint_invite_main(["--data-dir", data_dir])
    out = capsys.readouterr().out
    assert "token (send once" in out
    assert "invite id:" in out
