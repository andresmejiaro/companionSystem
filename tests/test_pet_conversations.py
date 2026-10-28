import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from profile_os.access import AccessControl
from profile_os.api import create_app
from profile_os.pet_conversations import RuntimeOutcomeUnknown
from profile_os.storage import Store


def seed(data_dir):
    store = Store(data_dir)
    for profile in ("hilo", "limo"):
        store.create_profile(profile, profile.title(), "identity", "role")
    store.close()


class EchoRuntime:
    def run(self, profile_id, history, text, identity, checkpoint):
        assert checkpoint()
        return f"{profile_id}: {text}"


@pytest.fixture
def env(tmp_path):
    data = str(tmp_path / "data")
    seed(data)
    app = create_app(data_dir=data, do_seed=False, pet_runtime=EchoRuntime())
    with TestClient(app) as client:
        issued = app.state.pet_devices.issue(["hilo"])
        headers = {"Authorization": f"Bearer {issued['secret']}"}
        yield app, client, headers, data, issued


def wait_turn(client, headers, turn_id, stage):
    for _ in range(100):
        result = client.get(f"/pet/v1/turns/{turn_id}", headers=headers).json()
        if result["stage"] == stage:
            return result
        time.sleep(0.01)
    pytest.fail(f"turn did not reach {stage}: {result}")


def conversation(client, headers, profile="hilo"):
    result = client.post("/pet/v1/conversations", headers=headers,
                         json={"profile_id": profile})
    assert result.status_code == 201
    return result.json()["conversation_id"]


def submit(client, headers, cid, request_id="r1", text="hello"):
    return client.post(f"/pet/v1/conversations/{cid}/turns", headers=headers,
                       json={"request_id": request_id, "text": text, "surface": "pet_text"})


def test_text_turn_deduplicates_and_persists_reply(env):
    _, client, headers, data, _ = env
    cid = conversation(client, headers)
    first = submit(client, headers, cid)
    assert first.status_code == 202
    done = wait_turn(client, headers, first.json()["turn_id"], "completed")
    assert done["reply_text"] == "hilo: hello" and done["revision"] >= 3
    duplicate = submit(client, headers, cid)
    assert duplicate.status_code == 200 and duplicate.json()["turn_id"] == done["turn_id"]
    assert submit(client, headers, cid, text="changed").status_code == 409
    assert client.get("/pet/v1/requests/r1", headers=headers).json()["reply_text"] == "hilo: hello"
    store = Store(data)
    assert store.db.execute("SELECT COUNT(*) n FROM pet_turns").fetchone()["n"] == 1
    store.close()


def test_unknown_request_blocks_conversation_after_restart(env):
    app, client, headers, data, issued = env
    cid = conversation(client, headers)
    accepted = submit(client, headers, cid).json()
    # Simulate a process crash after acceptance; startup recovery is idempotent.
    db = app.state.store.db
    with db:
        db.execute("UPDATE pet_turns SET stage='accepted' WHERE id=?", (accepted["turn_id"],))
    with app.state.store.db:
        app.state.store.db.execute("UPDATE pet_turns SET stage='unknown',revision=revision+1 WHERE id=?",
                                   (accepted["turn_id"],))
    status = client.get("/pet/v1/requests/r1", headers=headers)
    assert status.status_code == 503 and status.json()["outcome"] == "unknown"
    assert submit(client, headers, cid, request_id="r2").status_code == 409


def test_foreign_profile_and_revoked_owner_are_concealed(env):
    app, client, headers, _, issued = env
    assert client.post("/pet/v1/conversations", headers=headers,
                       json={"profile_id": "limo"}).status_code == 404
    cid = conversation(client, headers)
    app.state.pet_devices.revoke(issued["credential_id"])
    assert submit(client, headers, cid).status_code == 401


def test_cancel_while_runtime_runs_discards_late_reply(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def run(self, profile_id, history, text, identity, checkpoint):
            entered.set(); release.wait(2)
            return "late reply"

    data = str(tmp_path / "data"); seed(data)
    app = create_app(data_dir=data, do_seed=False, pet_runtime=Slow())
    with TestClient(app) as client:
        issued = app.state.pet_devices.issue(["hilo"])
        headers = {"Authorization": f"Bearer {issued['secret']}"}
        cid = conversation(client, headers)
        response = submit(client, headers, cid)
        tid = response.json()["turn_id"]
        assert entered.wait(1)
        cancel = client.post(f"/pet/v1/turns/{tid}/cancel", headers=headers, json={})
        assert cancel.json()["stage"] == "cancel_requested"
        release.set()
        cancelled = wait_turn(client, headers, tid, "cancelled")
        assert cancelled["reply_text"] is None


def test_runtime_unknown_and_unconfigured_capabilities(tmp_path):
    class Unknown:
        def run(self, *args): raise RuntimeOutcomeUnknown()

    data = str(tmp_path / "data"); seed(data)
    app = create_app(data_dir=data, do_seed=False, pet_runtime=Unknown())
    with TestClient(app) as client:
        issued = app.state.pet_devices.issue(["hilo"])
        headers = {"Authorization": f"Bearer {issued['secret']}"}
        cid = conversation(client, headers)
        response = submit(client, headers, cid)
        unknown = wait_turn(client, headers, response.json()["turn_id"], "unknown")
        polled = client.get("/pet/v1/requests/r1", headers=headers)
        assert polled.status_code == 503 and polled.json()["outcome"] == "unknown"
        assert unknown["error"] == "provider_outcome_unknown"

    data2 = str(tmp_path / "data2"); seed(data2)
    app2 = create_app(data_dir=data2, do_seed=False)
    with TestClient(app2) as client:
        issued = app2.state.pet_devices.issue(["hilo"])
        headers = {"Authorization": f"Bearer {issued['secret']}"}
        assert client.get("/pet/v1/capabilities", headers=headers).json()["text_turns"] is False
        cid = conversation(client, headers)
        assert submit(client, headers, cid).status_code == 503


def test_resume_requires_an_existing_conversation_owned_by_same_device(env):
    app, client, headers, _, _ = env
    cid = conversation(client, headers)
    resumed = client.post("/pet/v1/conversations", headers=headers,
                          json={"profile_id": "hilo", "conversation_id": cid})
    assert resumed.status_code == 200 and resumed.json()["conversation_id"] == cid
    assert client.post("/pet/v1/conversations", headers=headers,
                       json={"profile_id": "hilo", "conversation_id": "missing"}).status_code == 404
    other = app.state.pet_devices.issue(["hilo"])
    foreign = client.post("/pet/v1/conversations",
                          headers={"Authorization": f"Bearer {other['secret']}"},
                          json={"profile_id": "hilo", "conversation_id": cid})
    assert foreign.status_code == 404


def test_simultaneous_identical_submissions_share_one_durable_turn(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def run(self, profile_id, history, text, identity, checkpoint):
            entered.set(); release.wait(2)
            return "reply"

    data = str(tmp_path / "data"); seed(data)
    app = create_app(data_dir=data, do_seed=False, pet_runtime=Slow())
    with TestClient(app) as client:
        issued = app.state.pet_devices.issue(["hilo"])
        headers = {"Authorization": f"Bearer {issued['secret']}"}
        cid = conversation(client, headers)
        barrier = threading.Barrier(3)

        def send():
            barrier.wait()
            return submit(client, headers, cid)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(send); b = pool.submit(send); barrier.wait()
            first, second = a.result(), b.result()
        assert {first.status_code, second.status_code} == {200, 202}
        assert first.json()["turn_id"] == second.json()["turn_id"]
        assert entered.wait(1)
        blocked = submit(client, headers, cid, request_id="r2", text="second")
        assert blocked.status_code == 409
        release.set()


def test_revocation_during_runtime_prevents_reply_publication(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def run(self, profile_id, history, text, identity, checkpoint):
            entered.set(); release.wait(2)
            return "must not publish"

    data = str(tmp_path / "data"); seed(data)
    app = create_app(data_dir=data, do_seed=False, pet_runtime=Slow())
    with TestClient(app) as client:
        issued = app.state.pet_devices.issue(["hilo"])
        headers = {"Authorization": f"Bearer {issued['secret']}"}
        cid = conversation(client, headers)
        turn = submit(client, headers, cid).json()
        assert entered.wait(1)
        app.state.pet_devices.revoke(issued["credential_id"])
        release.set()
        for _ in range(100):
            row = app.state.store.db.execute("SELECT stage,reply_text,error FROM pet_turns WHERE id=?",
                                             (turn["turn_id"],)).fetchone()
            if row["stage"] == "failed": break
            time.sleep(0.01)
        assert row["stage"] == "failed" and row["reply_text"] is None
        assert row["error"] == "authorization_revoked"


def test_worker_reuses_thread_after_closing_its_store_connection(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    data = str(tmp_path / "data"); seed(data)
    app = create_app(data_dir=data, do_seed=False, pet_runtime=EchoRuntime())
    app.state.pet_turns.executor.shutdown(wait=True)
    app.state.pet_turns.executor = ThreadPoolExecutor(max_workers=1)
    with TestClient(app) as client:
        issued = app.state.pet_devices.issue(["hilo"])
        headers = {"Authorization": f"Bearer {issued['secret']}"}
        for index in range(3):
            cid = conversation(client, headers)
            response = submit(client, headers, cid, f"r{index}", f"hello {index}")
            assert response.status_code == 202
            done = wait_turn(client, headers, response.json()["turn_id"], "completed")
            assert done["reply_text"] == f"hilo: hello {index}"


def test_restart_marks_abandoned_accepted_turn_unknown(tmp_path):
    data = str(tmp_path / "data"); seed(data)
    first_app = create_app(data_dir=data, do_seed=False, pet_runtime=EchoRuntime())
    with TestClient(first_app) as client:
        issued = first_app.state.pet_devices.issue(["hilo"])
        headers = {"Authorization": f"Bearer {issued['secret']}"}
        cid = conversation(client, headers)
        # Hold dispatch while retaining a durable accepted row.
        first_app.state.pet_turns.executor.submit = lambda *args, **kwargs: None
        accepted = submit(client, headers, cid).json()
        assert accepted["stage"] == "accepted"

    second_app = create_app(data_dir=data, do_seed=False, pet_runtime=EchoRuntime())
    with TestClient(second_app) as client:
        headers = {"Authorization": f"Bearer {issued['secret']}"}
        result = client.get(f"/pet/v1/turns/{accepted['turn_id']}", headers=headers)
        assert result.json()["stage"] == "unknown"
