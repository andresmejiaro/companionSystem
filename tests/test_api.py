import pytest
from fastapi.testclient import TestClient

from profile_os.api import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"))
    with TestClient(app) as c:
        yield c


def test_list_and_get_profiles(client):
    ids = {p["id"] for p in client.get("/profiles").json()}
    assert {"sidra", "tara"} <= ids
    assert client.get("/profiles/tara").json()["display_name"] == "Tara"
    assert client.get("/profiles/ghost").status_code == 404


def test_boot_endpoint(client):
    r = client.post("/profiles/sidra/boot")
    assert r.status_code == 200
    body = r.json()
    assert body["base_prompt"] and body["compact_state"]


def test_remember_search_closeout_flow(client):
    r = client.post("/profiles/tara/memories",
                    json={"kind": "observation", "content": "ate paella at lunch",
                          "tags": ["lunch"]})
    assert r.status_code == 201
    hits = client.get("/profiles/tara/memories/search", params={"q": "paella"}).json()
    assert len(hits) == 1

    r = client.post("/profiles/tara/closeout",
                    json={"notes": "done", "new_state": "Paella day logged."})
    assert r.status_code == 201
    assert client.post("/profiles/tara/boot").json()["compact_state"] == "Paella day logged."


def test_malformed_event_via_api(client):
    r = client.post("/profiles/tara/memories",
                    json={"kind": "bogus", "content": "x"})
    assert r.status_code == 422


def test_domain_endpoints(client):
    r = client.get("/profiles/tara/domain/products", params={"contains": "granola"})
    assert r.status_code == 200 and len(r.json()) == 1
    r = client.post("/profiles/tara/domain/meals",
                    json={"data": {"food": "apple", "grams": 120}})
    assert r.status_code == 201
    assert r.json()["data"]["food"] == "apple"
    assert client.get("/profiles/ghost/domain/meals").status_code == 404


HOTEL_SCHEMA = {"fields": {"hotel_name": {"type": "string"},
                           "city": {"type": "string"},
                           "notes": {"type": "string", "required": False}}}


def test_dynamic_store_lifecycle_via_api(client):
    r = client.post("/profiles/tara/stores",
                    json={"name": "hotel_reservations", "purpose": "bookings",
                          "proposed_by": "tara", "schema": HOTEL_SCHEMA})
    assert r.status_code == 201 and r.json()["status"] == "pending"

    # pending: writes blocked with 409
    r = client.post("/profiles/tara/stores/hotel_reservations/records",
                    json={"data": {"hotel_name": "X", "city": "Y"}})
    assert r.status_code == 409

    assert client.post("/profiles/tara/stores/hotel_reservations/approve").json()["status"] == "approved"

    r = client.post("/profiles/tara/stores/hotel_reservations/records",
                    json={"data": {"hotel_name": "Grand", "city": "Sevilla"}})
    assert r.status_code == 201

    # invalid record → 422
    r = client.post("/profiles/tara/stores/hotel_reservations/records",
                    json={"data": {"hotel_name": "Grand", "city": "Sevilla", "stars": 5}})
    assert r.status_code == 422

    hits = client.get("/profiles/tara/stores/hotel_reservations/records",
                      params={"contains": "sevilla"}).json()
    assert len(hits) == 1

    # profile scoping: sidra sees no such store
    assert client.get("/profiles/sidra/stores/hotel_reservations/records").status_code == 404

    # audit + listing
    actions = {e["action"] for e in
               client.get("/profiles/tara/stores/hotel_reservations/audit").json()}
    assert {"proposed", "approved"} <= actions
    names = {s["name"] for s in client.get("/profiles/tara/stores").json()}
    assert "hotel_reservations" in names

    assert client.post("/profiles/tara/stores/hotel_reservations/archive").json()["status"] == "archived"
    assert client.get("/profiles/tara/stores/hotel_reservations/records").status_code == 200


def test_invalid_schema_via_api(client):
    r = client.post("/profiles/tara/stores",
                    json={"name": "bad", "purpose": "p", "proposed_by": "tara",
                          "schema": {"fields": {"x": {"type": "blob"}}}})
    assert r.status_code == 422
