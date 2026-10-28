"""Run inside the deployed backend; never print credentials or profile data."""
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from profile_os.access import AccessControl
from profile_os.pet_devices import PetDeviceService
from profile_os.storage import Store


def request(path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urlopen(Request("http://127.0.0.1:8000" + path, data=data,
                             headers=headers), timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, None


assert os.environ.get("PROFILE_OS_AUTH_ENABLED") == "1"
assert os.environ.get("PET_RUNTIME_ENABLED") == "0"
assert request("/health")[0] == 200
assert request("/profiles")[0] == 401
assert request("/pet/v1/capabilities")[0] == 401
bridge = os.environ["PROFILE_OS_MCP_BACKEND_BEARER"]
assert request("/profiles", bridge)[0] == 200
assert request("/pet/v1/capabilities", bridge)[0] == 401

store = Store(os.environ["PROFILE_OS_DATA_DIR"])
devices = PetDeviceService(AccessControl(store))
issued = None
try:
    profile = store.db.execute(
        "SELECT id FROM profiles WHERE id IN ('hilo','limo') "
        "AND profile_kind='companion' ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    issued = devices.issue([profile], expires_in=300,
                           device_name="tamagochi-deployment-smoke")
    status, caps = request("/pet/v1/capabilities", issued["secret"])
    assert status == 200
    assert caps["text_turns"] is False and caps["voice_turns"] is False
    status, conversation = request("/pet/v1/conversations", issued["secret"],
                                   {"profile_id": profile})
    assert status == 201
    status, _ = request(
        "/pet/v1/conversations/" + conversation["conversation_id"] + "/turns",
        issued["secret"], {"request_id": "deployment-disabled-check",
                           "text": "Deployment health check.", "surface": "pet_text"})
    assert status == 503
    devices.revoke(issued["credential_id"])
    assert request("/pet/v1/capabilities", issued["secret"])[0] == 401
    print(json.dumps({"api_health": "ok", "api_auth": "enforced",
                      "bridge_access": "ok", "pet_auth": "enforced",
                      "bridge_rejected_by_pet": True,
                      "text_turns": False, "voice_turns": False,
                      "text_submission": 503, "smoke_device_revoked": True}))
finally:
    if issued is not None:
        row = store.db.execute("SELECT revoked_at FROM pet_devices WHERE credential_id=?",
                               (issued["credential_id"],)).fetchone()
        if row and row["revoked_at"] is None:
            devices.revoke(issued["credential_id"])
    store.close()
