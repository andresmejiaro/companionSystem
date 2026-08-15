from fastapi.testclient import TestClient

from profile_os.api import create_app


def _continuity(**overrides):
    row = {
        "discussion_id": "26",
        "source_type": "reply",
        "source_id": "reply-184",
        "subject": "Unverified success states",
        "position": "A 204 means accepted, not behaviorally verified.",
        "stake": "Telemetry must distinguish success from unknown.",
        "open_loop": "Will the forum adopt explicit unknown states?",
        "status": "active",
        "occurred_at": "2026-08-14T20:15:00Z",
        "updated_at": "2026-08-14T20:15:00Z",
    }
    row.update(overrides)
    return row


def test_system_continuity_store_is_available_and_upserts_by_source(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"))
    with TestClient(app) as client:
        stores = client.get("/profiles/tara/stores").json()
        continuity = next(item for item in stores
                          if item["name"] == "thread_continuity")
        assert continuity["status"] == "approved"
        assert continuity["schema"]["fields"]["position"]["type"] == "string"

        created = client.post(
            "/profiles/tara/stores/thread_continuity/records",
            json={"data": _continuity()},
        )
        assert created.status_code == 201

        retried = client.post(
            "/profiles/tara/stores/thread_continuity/records",
            json={"data": _continuity(
                position="A 204 is only acceptance until readback.",
                updated_at="2026-08-15T08:00:00Z",
            )},
        )
        assert retried.status_code == 201
        assert retried.json()["id"] == created.json()["id"]

        records = client.get(
            "/profiles/tara/stores/thread_continuity/records").json()
        assert len(records) == 1
        assert records[0]["data"]["position"].endswith("until readback.")


def test_continuity_contract_rejects_invalid_state_and_normal_session_omits_rows(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"))
    with TestClient(app) as client:
        invalid = client.post(
            "/profiles/tara/stores/thread_continuity/records",
            json={"data": _continuity(status="forgotten")},
        )
        assert invalid.status_code == 422

        assert client.post(
            "/profiles/tara/stores/thread_continuity/records",
            json={"data": _continuity()},
        ).status_code == 201
        session = client.post("/profiles/tara/session").json()
        assert "thread_continuity" not in session

        record = client.get(
            "/profiles/tara/stores/thread_continuity/records").json()[0]
        changed_source = client.patch(
            f"/profiles/tara/stores/thread_continuity/records/{record['id']}",
            json={"patch": {"source_id": "different-reply"}},
        )
        assert changed_source.status_code == 422

        bulk = client.post(
            "/profiles/tara/stores/thread_continuity/records/bulk",
            json={"records": [_continuity(source_id="bulk-reply")]},
        )
        assert bulk.status_code == 409
