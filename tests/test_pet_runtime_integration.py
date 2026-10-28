import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.pet_runtime import PetResponsesRuntime
from profile_os.storage import Store


def _seed(data_dir):
    store = Store(data_dir)
    for profile in ("hilo", "limo"):
        store.create_profile(profile, profile.title(), "synthetic identity", "synthetic role")
    store.close()


def _wait_turn(client, headers, turn_id, stage):
    for _ in range(200):
        result = client.get(f"/pet/v1/turns/{turn_id}", headers=headers).json()
        if result["stage"] == stage:
            return result
        time.sleep(0.01)
    pytest.fail(f"turn did not reach {stage}: {result}")


def _issue(app, profiles=("hilo", "limo")):
    issued = app.state.pet_devices.issue(list(profiles))
    return issued, {"Authorization": f"Bearer {issued['secret']}"}


def _conversation(client, headers, profile="hilo"):
    response = client.post("/pet/v1/conversations", headers=headers,
                           json={"profile_id": profile})
    assert response.status_code == 201
    return response.json()["conversation_id"]


def _submit(client, headers, cid, request_id, text):
    return client.post(f"/pet/v1/conversations/{cid}/turns", headers=headers,
                       json={"request_id": request_id, "text": text,
                             "surface": "pet_text"})


def _message(text):
    return {"type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text}]}


def _runtime_factory(monkeypatch, handler, models=None):
    captured = {}

    def factory(store, dynstores, devices, identity_file):
        captured.update(store=store, dynstores=dynstores, devices=devices,
                        identity_file=identity_file)
        transport = httpx.MockTransport(handler)
        captured["transport"] = transport
        return PetResponsesRuntime(
            store, dynstores, devices, "synthetic-test-key",
            models or {"hilo": "synthetic-model"},
            "https://provider.invalid/v1/responses", identity_file,
            client=httpx.AsyncClient(transport=transport),
        )

    monkeypatch.setattr("profile_os.api.runtime_from_environment", factory)
    return captured


def test_factory_wiring_capabilities_history_and_http_dedup(monkeypatch, tmp_path):
    _seed(str(tmp_path / "data"))
    monkeypatch.setenv("PET_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("PET_OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("PET_MODEL_HILO", "synthetic-model")
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["authorization"] == "Bearer synthetic-test-key"
        return httpx.Response(200, json={"status": "completed",
                                         "output": [_message(f"reply-{len(requests)}")]})

    captured = _runtime_factory(monkeypatch, handler)
    app = create_app(data_dir=str(tmp_path / "data"), do_seed=False,
                     identity_file="/synthetic/identity.txt")
    with TestClient(app) as client:
        assert captured["store"] is app.state.store
        assert captured["dynstores"] is app.state.dynstores
        assert captured["devices"] is app.state.pet_devices
        assert captured["identity_file"] == "/synthetic/identity.txt"
        issued, headers = _issue(app)
        caps = client.get("/pet/v1/capabilities", headers=headers).json()
        assert caps["profiles"] == ["hilo", "limo"]
        assert caps["text_turns"] is True

        cid = _conversation(client, headers)
        first = _submit(client, headers, cid, "synthetic-r1", "hello")
        done1 = _wait_turn(client, headers, first.json()["turn_id"], "completed")
        assert done1["reply_text"] == "reply-1"
        second = _submit(client, headers, cid, "synthetic-r2", "follow up")
        done2 = _wait_turn(client, headers, second.json()["turn_id"], "completed")
        assert done2["reply_text"] == "reply-2"
        duplicate = _submit(client, headers, cid, "synthetic-r2", "follow up")
        assert duplicate.status_code == 200
        assert duplicate.json()["turn_id"] == done2["turn_id"]
        assert len(requests) == 2
        assert requests[0]["input"] == [{"role": "user", "content": "hello"}]
        assert requests[1]["input"] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "reply-1"},
            {"role": "user", "content": "follow up"},
        ]
        assert requests[0]["model"] == requests[1]["model"] == "synthetic-model"
        # The factory only configured Hilo; Limo remains outside its capability.
        assert client.post("/pet/v1/conversations", headers=headers,
                           json={"profile_id": "limo"}).status_code == 201
        limo = _conversation(client, headers, "limo")
        assert _submit(client, headers, limo, "synthetic-limo", "hello").status_code == 503
        assert len(requests) == 2
    assert app.state.pet_turns.runtime._closed is True


def test_api_key_settings_leave_real_app_runtime_disabled(monkeypatch, tmp_path):
    _seed(str(tmp_path / "data"))
    monkeypatch.setenv("PET_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("PET_OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("PET_MODEL_HILO", "synthetic-hilo")
    monkeypatch.delenv("PET_MODEL_LIMO", raising=False)
    app = create_app(data_dir=str(tmp_path / "data"), do_seed=False)
    with TestClient(app) as client:
        _, headers = _issue(app, ("hilo",))
        assert app.state.pet_turns.runtime is None
        caps = client.get("/pet/v1/capabilities", headers=headers).json()
        assert caps["text_turns"] is False and caps["voice_turns"] is False
        cid = _conversation(client, headers)
        assert _submit(client, headers, cid, "no-api-runtime", "hello").status_code == 503


def test_timeout_is_unknown_and_same_request_never_replays(monkeypatch, tmp_path):
    _seed(str(tmp_path / "data"))
    calls = []

    async def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic timeout")

    _runtime_factory(monkeypatch, handler)
    app = create_app(data_dir=str(tmp_path / "data"), do_seed=False)
    with TestClient(app) as client:
        _, headers = _issue(app, ("hilo",))
        cid = _conversation(client, headers)
        accepted = _submit(client, headers, cid, "synthetic-timeout", "hello")
        unknown = _wait_turn(client, headers, accepted.json()["turn_id"], "unknown")
        assert unknown["reply_text"] is None
        assert client.get("/pet/v1/requests/synthetic-timeout",
                          headers=headers).status_code == 503
        duplicate = _submit(client, headers, cid, "synthetic-timeout", "hello")
        assert duplicate.status_code == 200
        assert duplicate.json()["stage"] == "unknown"
        assert len(calls) == 1


def test_revocation_before_tool_dispatch_prevents_tool_and_publication(monkeypatch, tmp_path):
    _seed(str(tmp_path / "data"))
    app_ref = {}
    calls = []

    async def handler(request):
        calls.append(request)
        # Revoke after the provider asks for a read and before local dispatch.
        app = app_ref["app"]
        app.state.pet_devices.revoke(app_ref["issued"]["credential_id"])
        return httpx.Response(200, json={"status": "completed", "output": [{
            "type": "function_call", "name": "search_memories",
            "call_id": "synthetic-call", "arguments": json.dumps({"query": "secret", "limit": 1})
        }]})

    _runtime_factory(monkeypatch, handler)
    app = create_app(data_dir=str(tmp_path / "data"), do_seed=False)
    app_ref["app"] = app
    searched = []
    original_search = app.state.store.search

    def search(*args, **kwargs):
        searched.append(args)
        return original_search(*args, **kwargs)

    app.state.store.search = search
    with TestClient(app) as client:
        issued, headers = _issue(app, ("hilo",))
        app_ref["issued"] = issued
        cid = _conversation(client, headers)
        accepted = _submit(client, headers, cid, "synthetic-revoked", "hello")
        for _ in range(200):
            row = app.state.store.db.execute(
                "SELECT stage,reply_text,error FROM pet_turns WHERE id=?",
                (accepted.json()["turn_id"],)).fetchone()
            if row["stage"] in ("failed", "completed", "unknown"):
                break
            time.sleep(0.01)
        assert row["stage"] == "failed"
        assert row["reply_text"] is None
        assert row["error"] == "authorization_revoked"
        assert searched == []
        assert len(calls) == 1


def test_revocation_before_final_publication_discards_reply(monkeypatch, tmp_path):
    _seed(str(tmp_path / "data"))
    app_ref = {}

    async def handler(request):
        app_ref["app"].state.pet_devices.revoke(app_ref["issued"]["credential_id"])
        return httpx.Response(200, json={"status": "completed",
                                         "output": [_message("synthetic late reply")]})

    _runtime_factory(monkeypatch, handler)
    app = create_app(data_dir=str(tmp_path / "data"), do_seed=False)
    app_ref["app"] = app
    with TestClient(app) as client:
        issued, headers = _issue(app, ("hilo",))
        app_ref["issued"] = issued
        cid = _conversation(client, headers)
        accepted = _submit(client, headers, cid, "synthetic-publish-revoke", "hello")
        turn_id = accepted.json()["turn_id"]
        for _ in range(200):
            row = app.state.store.db.execute(
                "SELECT stage,reply_text,error FROM pet_turns WHERE id=?",
                (turn_id,)).fetchone()
            if row["stage"] in ("failed", "completed", "unknown"):
                break
            time.sleep(0.01)
        assert row["reply_text"] is None
        assert row["stage"] == "failed"
        assert row["error"] == "authorization_revoked"
