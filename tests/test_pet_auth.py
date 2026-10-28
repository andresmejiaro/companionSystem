import re

import pytest
from fastapi.testclient import TestClient

from profile_os.access import AccessControl
from profile_os.api import create_app
from profile_os.pet_devices import PetDeviceService
from profile_os.storage import Store


def _profiles(data_dir):
    store = Store(str(data_dir))
    for profile in ("hilo", "limo"):
        store.create_profile(profile, profile.title(), "identity", "role")
    store.close()


@pytest.fixture
def app_client(tmp_path):
    data_dir = tmp_path / "data"
    _profiles(data_dir)
    app = create_app(data_dir=str(data_dir), do_seed=False, auth_enabled=False)
    with TestClient(app) as client:
        yield app, client, data_dir


def test_pet_route_requires_registered_device_even_when_api_auth_disabled(app_client):
    _, client, data_dir = app_client
    assert client.get("/pet/v1/capabilities").status_code == 401
    store = Store(str(data_dir))
    access = AccessControl(store)
    bridge = access.create_principal("bridge", "broad bridge")
    token = "broad-bridge-secret"
    access.create_credential(bridge["id"], "bridge", token)
    for profile in ("hilo", "limo"):
        for operation in ("pet:converse", "boot", "search", "records:read"):
            access.grant(bridge["id"], operation, profile)
    store.close()
    assert client.get("/pet/v1/capabilities", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_capabilities_filters_profiles_and_never_discloses_token(app_client):
    app, client, _ = app_client
    issued = app.state.pet_devices.issue(["hilo"])
    response = client.get("/pet/v1/capabilities", headers={
        "Authorization": f"Bearer {issued['secret']}"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["profiles"] == ["hilo"]
    assert payload["text_turns"] is False and payload["voice_turns"] is False
    assert issued["secret"] not in response.text


def test_each_request_rechecks_grants_expiry_revocation_and_disabled_principal(app_client):
    app, client, _ = app_client
    issued = app.state.pet_devices.issue(["hilo"])
    headers = {"Authorization": f"Bearer {issued['secret']}"}
    assert client.get("/pet/v1/capabilities", headers=headers).status_code == 200
    grants = app.state.access.list_grants(issued["principal_id"])
    app.state.access.revoke_grant(next(g["id"] for g in grants if g["operation"] == "search"))
    assert client.get("/pet/v1/capabilities", headers=headers).json()["profiles"] == []
    app.state.access.disable_principal(issued["principal_id"])
    assert client.get("/pet/v1/capabilities", headers=headers).status_code == 401

    issued2 = app.state.pet_devices.issue(["limo"])
    with app.state.access.db:
        app.state.access.db.execute("UPDATE access_credentials SET expires_at=? WHERE id=?",
                                    (0, issued2["credential_id"]))
    assert client.get("/pet/v1/capabilities", headers={
        "Authorization": f"Bearer {issued2['secret']}"}).status_code == 401

    issued3 = app.state.pet_devices.issue(["limo"])
    app.state.pet_devices.revoke(issued3["credential_id"])
    assert client.get("/pet/v1/capabilities", headers={
        "Authorization": f"Bearer {issued3['secret']}"}).status_code == 401


def test_cli_issue_and_revoke_with_synthetic_profiles(tmp_path, capsys):
    from profile_os.pet_devices_cli import main

    data_dir = tmp_path / "synthetic"
    _profiles(data_dir)
    main(["--data-dir", str(data_dir), "issue", "--profiles", "hilo",
          "--expires-hours", "1"])
    output = capsys.readouterr().out
    token = re.search(r"bearer .*: (\S+)", output).group(1)
    credential_id = re.search(r"credential_id: ([\w-]+)", output).group(1)
    store = Store(str(data_dir))
    access = AccessControl(store)
    principal_id = access.get_credential(credential_id)["principal_id"]
    assert access.authenticate_secret(token) == principal_id
    assert access.db.execute("SELECT secret_hash FROM access_credentials WHERE id=?",
                             (credential_id,)).fetchone()["secret_hash"] != token
    store.close()
    main(["--data-dir", str(data_dir), "revoke", credential_id])
    capsys.readouterr()
    store = Store(str(data_dir))
    assert PetDeviceService(AccessControl(store)).authenticate(token) is None
    store.close()


def test_invalid_profile_issue_does_not_create_partial_principal(app_client):
    app, _, _ = app_client
    before = app.state.access.db.execute("SELECT COUNT(*) n FROM access_principals").fetchone()["n"]
    with pytest.raises(ValueError):
        app.state.pet_devices.issue(["unknown"])
    after = app.state.access.db.execute("SELECT COUNT(*) n FROM access_principals").fetchone()["n"]
    assert before == after


def test_database_failure_rolls_back_entire_device_issue(app_client):
    app, _, _ = app_client
    db = app.state.access.db
    db.execute("CREATE TRIGGER reject_pet_grant BEFORE INSERT ON access_grants "
               "WHEN NEW.operation='pet:converse' BEGIN SELECT RAISE(ABORT,'rejected'); END")
    with pytest.raises(Exception, match="rejected"):
        app.state.pet_devices.issue(["hilo"])
    for table in ("access_principals", "access_credentials", "access_grants", "pet_devices"):
        count = db.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
        assert count == 0
