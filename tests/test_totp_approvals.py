import pyotp
import pytest
from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.bootstrap_admin import bootstrap as bootstrap_admin


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def auth_client(tmp_path):
    data_dir = str(tmp_path / "data")
    admin = bootstrap_admin(data_dir, "root-secret")
    app = create_app(data_dir=data_dir, auth_enabled=True)
    with TestClient(app) as c:
        yield c, app.state.access, admin["principal_id"]


class _FakeClock:
    """Advances a shared clock that both the test's code generator and
    access.py's time.time() see, so each 'next code' is a genuinely later
    TOTP window — real usage never confirms and verifies within the same
    30s window, and TOTP's drift tolerance can't be faked with a future
    timestamp alone (server-side verification checks against its own now)."""

    def __init__(self, monkeypatch, start: float):
        self._t = start
        monkeypatch.setattr("profile_os.access.time.time", lambda: self._t)

    def advance(self, seconds: float = 31) -> None:
        self._t += seconds

    def next_code(self, totp: pyotp.TOTP) -> str:
        self.advance()
        return totp.at(self._t)


@pytest.fixture
def clock(monkeypatch):
    import time as _time
    return _FakeClock(monkeypatch, _time.time())


def _enroll_and_confirm(access, principal_id, clock):
    uri = access.enroll_totp(principal_id)
    secret = dict(pyotp.parse_uri(uri).__dict__)["secret"]
    totp = pyotp.TOTP(secret)
    assert access.confirm_totp(principal_id, clock.next_code(totp))
    return totp


def test_totp_enroll_confirm_and_verify(auth_client, clock):
    client, access, admin_id = auth_client
    assert not access.has_totp(admin_id)
    totp = _enroll_and_confirm(access, admin_id, clock)
    assert access.has_totp(admin_id)
    assert access.verify_totp(admin_id, clock.next_code(totp))


def test_totp_code_is_single_use(auth_client, clock):
    client, access, admin_id = auth_client
    totp = _enroll_and_confirm(access, admin_id, clock)
    code = clock.next_code(totp)
    assert access.verify_totp(admin_id, code)
    assert not access.verify_totp(admin_id, code)  # replay rejected


def test_totp_wrong_code_fails(auth_client, clock):
    client, access, admin_id = auth_client
    _enroll_and_confirm(access, admin_id, clock)
    assert not access.verify_totp(admin_id, "000000")


def test_totp_unconfirmed_secret_never_verifies(auth_client, clock):
    client, access, admin_id = auth_client
    uri = access.enroll_totp(admin_id)
    secret = dict(pyotp.parse_uri(uri).__dict__)["secret"]
    code = clock.next_code(pyotp.TOTP(secret))
    assert not access.verify_totp(admin_id, code)  # never confirmed


def test_prompt_edit_propose_and_approve_flow(auth_client, clock):
    client, access, admin_id = auth_client
    totp = _enroll_and_confirm(access, admin_id, clock)

    owner = access.create_principal("agent", "rumbo-owner")
    access.create_credential(owner["id"], "k", "owner-secret")
    access.grant(owner["id"], "manage_profile", profile_id="tara")
    access.grant(owner["id"], "boot", profile_id="tara")

    r = client.post("/profiles/tara/prompt", headers=_bearer("owner-secret"),
                    json={"base_prompt": "New base prompt."})
    assert r.status_code == 201, r.text
    approval_id = r.json()["id"]
    assert r.json()["status"] == "pending"

    listed = client.get("/approvals", headers=_bearer("root-secret"))
    assert listed.status_code == 200
    assert any(a["id"] == approval_id for a in listed.json())

    bad = client.post(f"/approvals/{approval_id}/decide", headers=_bearer("root-secret"),
                      json={"approve": True, "totp_code": "000000"})
    assert bad.status_code == 401

    ok = client.post(f"/approvals/{approval_id}/decide", headers=_bearer("root-secret"),
                     json={"approve": True, "totp_code": clock.next_code(totp)})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"

    # applied to the actual profile prompt
    booted = client.post("/profiles/tara/boot", headers=_bearer("owner-secret"))
    assert booted.json()["base_prompt"] == "New base prompt."

    # can't decide twice
    again = client.post(f"/approvals/{approval_id}/decide", headers=_bearer("root-secret"),
                        json={"approve": True, "totp_code": clock.next_code(totp)})
    assert again.status_code == 409


def test_prompt_edit_rejection_needs_no_code(auth_client, clock):
    client, access, admin_id = auth_client
    _enroll_and_confirm(access, admin_id, clock)
    owner = access.create_principal("agent", "vera-owner")
    access.create_credential(owner["id"], "k", "owner-secret")
    access.grant(owner["id"], "manage_profile", profile_id="tara")
    access.grant(owner["id"], "boot", profile_id="tara")

    r = client.post("/profiles/tara/prompt", headers=_bearer("owner-secret"),
                    json={"role_prompt": "nope"})
    approval_id = r.json()["id"]
    reject = client.post(f"/approvals/{approval_id}/decide", headers=_bearer("root-secret"),
                         json={"approve": False})
    assert reject.status_code == 200
    assert reject.json()["status"] == "rejected"

    booted = client.post("/profiles/tara/boot", headers=_bearer("owner-secret"))
    assert booted.json()["role_prompt"] != "nope"


def test_prompt_edit_requires_manage_profile_grant(auth_client):
    client, access, admin_id = auth_client
    p = access.create_principal("agent", "no-owner")
    access.create_credential(p["id"], "k", "no-owner-secret")
    r = client.post("/profiles/tara/prompt", headers=_bearer("no-owner-secret"),
                    json={"base_prompt": "x"})
    assert r.status_code == 403


def test_decide_without_totp_enrolled_is_403(auth_client):
    client, access, admin_id = auth_client
    owner = access.create_principal("agent", "rita-owner")
    access.create_credential(owner["id"], "k", "owner-secret")
    access.grant(owner["id"], "manage_profile", profile_id="tara")
    r = client.post("/profiles/tara/prompt", headers=_bearer("owner-secret"),
                    json={"base_prompt": "x"})
    approval_id = r.json()["id"]
    # admin has not enrolled TOTP yet in this test
    decide = client.post(f"/approvals/{approval_id}/decide", headers=_bearer("root-secret"),
                         json={"approve": True, "totp_code": "123456"})
    assert decide.status_code == 403


def test_start_session_bundles_identity_closeouts_and_all_memories(tmp_path, monkeypatch):
    identity_path = tmp_path / "quien_soy.md"
    identity_path.write_text("canonical facts")
    monkeypatch.setenv("PROFILE_OS_IDENTITY_FILE", str(identity_path))
    data_dir = str(tmp_path / "data")
    admin = bootstrap_admin(data_dir, "root-secret")
    app = create_app(data_dir=data_dir, auth_enabled=True, do_seed=False)
    with TestClient(app) as client:
        access = app.state.access
        access.grant(admin["principal_id"], "create_profile", profile_id=None)
        client.post("/profiles", headers=_bearer("root-secret"),
                   json={"id": "tara", "display_name": "Tara"})
        access.grant(admin["principal_id"], "identity:read", profile_id=None)
        access.grant(admin["principal_id"], "boot", profile_id="tara")
        access.grant(admin["principal_id"], "remember", profile_id="tara")
        access.grant(admin["principal_id"], "closeout", profile_id="tara")
        h = _bearer("root-secret")

        for i in range(15):
            client.post("/profiles/tara/memories", headers=h,
                       json={"kind": "note", "content": f"memory {i}"})
        client.post("/profiles/tara/closeout", headers=h,
                   json={"notes": "n1", "new_state": "state1"})
        client.post("/profiles/tara/closeout", headers=h,
                   json={"notes": "n2", "new_state": "state2"})
        client.post("/profiles/tara/closeout", headers=h,
                   json={"notes": "n3", "new_state": "state3"})

        r = client.post("/profiles/tara/session", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["identity"] == "canonical facts"
        assert len(body["memories"]) == 15  # not capped at DEFAULT_BOOT_EVENTS
        assert len(body["last_closeouts"]) == 2
        assert [c["new_state"] for c in body["last_closeouts"]] == ["state3", "state2"]
        assert "recent_memories" not in body


def test_start_session_omits_identity_without_grant(tmp_path, monkeypatch):
    identity_path = tmp_path / "quien_soy.md"
    identity_path.write_text("canonical facts")
    monkeypatch.setenv("PROFILE_OS_IDENTITY_FILE", str(identity_path))
    data_dir = str(tmp_path / "data")
    app = create_app(data_dir=data_dir, auth_enabled=True)
    with TestClient(app) as client:
        access = app.state.access
        p = access.create_principal("app", "booter")
        access.create_credential(p["id"], "k", "boot-secret")
        access.grant(p["id"], "boot", profile_id="tara")
        r = client.post("/profiles/tara/session", headers=_bearer("boot-secret"))
        assert r.status_code == 200
        assert r.json()["identity"] is None
