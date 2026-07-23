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


def test_profile_discovery_metadata_and_startup_routing_guidance(client):
    created = client.post("/profiles", json={
        "id": "travel", "display_name": "Travel", "description": "Plans trips.",
        "signature": "🧭", "base_prompt": "", "role_prompt": "",
    })
    assert created.status_code == 201
    assert created.json()["signature"] == "🧭"

    updated = client.put("/profiles/travel/description", json={"signature": "✈️"})
    assert updated.status_code == 200
    assert updated.json()["description"] == "Plans trips."
    assert updated.json()["signature"] == "✈️"

    session = client.post("/profiles/tara/session").json()
    assert "outside your lane" in session["routing_guidance"]
    assert "Travel ✈️ — Plans trips." in session["routing_guidance"]

    assert client.post("/profiles", json={
        "id": "too_long", "display_name": "Too long", "description": "x" * 201,
    }).status_code == 422
    assert client.put("/profiles/travel/description", json={"signature": "abcdef"}).status_code == 422


def test_root_directory_and_admin_shortcuts(client):
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/directory"

    directory = client.get("/directory")
    assert directory.status_code == 200
    assert "Companion directory" in directory.text
    assert 'href="/companions/new"' in directory.text
    assert 'href="/settings"' in directory.text

    new = client.get("/companions/new")
    assert new.status_code == 200
    assert "New companion" in new.text
    assert "/profiles/totp-create" in new.text
    preset = client.get("/companions/new?template=system_notifier")
    assert "System Notifier" in preset.text
    assert "allowed_tools: 'send_message'" in preset.text
    settings = client.get("/settings")
    assert settings.status_code == 200
    assert "Unlock settings" in settings.text
    legacy_demo = client.get("/demo", follow_redirects=False)
    assert legacy_demo.status_code == 307
    assert legacy_demo.headers["location"] == "/settings"


def test_boot_endpoint(client):
    r = client.post("/profiles/sidra/boot")
    assert r.status_code == 200
    body = r.json()
    assert body["base_prompt"] and body["compact_state"]


def test_session_inspect_matches_start_session_shape_when_auth_is_disabled(client):
    inspected = client.post("/profiles/sidra/session-inspect", json={"totp_code": "123456"})
    assert inspected.status_code == 200
    body = inspected.json()
    assert {"profile", "base_prompt", "role_prompt", "compact_state", "identity",
            "memories", "recent_exchanges", "you_got_mail", "server_time"} <= set(body)
    assert body["you_got_mail"] is False
    assert "last_closeouts" not in body


def test_start_session_you_got_mail_tracks_unread_inbox(client):
    sent = client.post("/profiles/tara/messages", json={
        "to_profile_id": "sidra", "content": "Project update"})
    assert sent.status_code == 201
    assert client.post("/profiles/sidra/session").json()["you_got_mail"] is True

    message_id = sent.json()["id"]
    assert client.post(f"/profiles/sidra/inbox/{message_id}/read").status_code == 200
    assert client.post("/profiles/sidra/session").json()["you_got_mail"] is False


def test_remember_search_closeout_flow(client):
    r = client.post("/profiles/tara/memories",
                    json={"kind": "observation", "content": "ate paella at lunch",
                          "tags": ["lunch"]})
    assert r.status_code == 201
    hits = client.get("/profiles/tara/memories/search", params={"q": "paella"}).json()
    assert len(hits) == 1

    r = client.post("/profiles/tara/closeout",
                    json={"facts": "Paella day logged.", "texture": "Ordinary meal logging.",
                          "exchange": "User: logged paella.\nTara: Recorded.", "notes": "done"})
    assert r.status_code == 201
    assert "Paella day logged." in client.post("/profiles/tara/boot").json()["compact_state"]


def test_start_session_includes_four_recent_interaction_anchors(client):
    for number in range(5):
        response = client.post("/profiles/tara/closeout", json={
            "facts": f"Fact {number}",
            "texture": f"Texture {number}",
            "exchange": f"User: Example {number}.\nTara: Reply {number}.",
        })
        assert response.status_code == 201

    anchors = client.post("/profiles/tara/session").json()["recent_exchanges"]
    assert anchors == [
        {"texture": f"Texture {number}",
         "exchange": f"User: Example {number}.\nTara: Reply {number}."}
        for number in range(1, 5)
    ]


def test_update_and_delete_memory_via_api(client):
    r = client.post("/profiles/tara/memories",
                    json={"kind": "note", "content": "original", "tags": ["a"]})
    assert r.status_code == 201
    event_id = r.json()["id"]

    upd = client.patch(f"/profiles/tara/memories/{event_id}",
                       json={"content": "revised", "tags": ["b", "c"]})
    assert upd.status_code == 200, upd.text
    assert upd.json()["content"] == "revised"
    assert upd.json()["tags"] == ["b", "c"]
    assert upd.json()["kind"] == "note"  # untouched field preserved

    hits = client.get("/profiles/tara/memories/search", params={"q": "revised"}).json()
    assert len(hits) == 1
    assert not client.get("/profiles/tara/memories/search", params={"q": "original"}).json()

    empty = client.patch(f"/profiles/tara/memories/{event_id}", json={})
    assert empty.status_code == 422

    bad_kind = client.patch(f"/profiles/tara/memories/{event_id}", json={"kind": "bogus"})
    assert bad_kind.status_code == 422

    unknown = client.patch("/profiles/tara/memories/does-not-exist", json={"content": "x"})
    assert unknown.status_code == 404

    d = client.delete(f"/profiles/tara/memories/{event_id}")
    assert d.status_code == 204
    assert not client.get("/profiles/tara/memories/search", params={"q": "revised"}).json()

    d_again = client.delete(f"/profiles/tara/memories/{event_id}")
    assert d_again.status_code == 404


def test_file_store_write_read_list_delete(client):
    w = client.put("/profiles/tara/files/notes.md", json={"content": "hello world"})
    assert w.status_code == 201, w.text
    assert w.json()["filename"] == "notes.md"
    assert w.json()["size"] == len(b"hello world")

    listed = client.get("/profiles/tara/files").json()
    assert [f["filename"] for f in listed] == ["notes.md"]

    r = client.get("/profiles/tara/files/notes.md")
    assert r.status_code == 200
    assert r.json()["content"] == "hello world"

    # overwrite
    w2 = client.put("/profiles/tara/files/notes.md", json={"content": "updated"})
    assert w2.status_code == 201
    assert client.get("/profiles/tara/files/notes.md").json()["content"] == "updated"

    # separate profile has its own isolated store
    assert client.get("/profiles/sidra/files").json() == []

    d = client.delete("/profiles/tara/files/notes.md")
    assert d.status_code == 204
    assert client.get("/profiles/tara/files").json() == []
    assert client.get("/profiles/tara/files/notes.md").status_code == 404
    assert client.delete("/profiles/tara/files/notes.md").status_code == 404


def test_file_store_path_traversal_rejected(client, tmp_path):
    # "a/b" and "/etc/passwd" don't even reach our handler (multi-segment
    # paths 404 at routing); ".." is a single segment and must be rejected
    # explicitly by _validate_filename, so exercise that directly too.
    from profile_os.errors import MalformedRecord
    from profile_os.storage import Store

    for bad_name in ("a/../../etc/passwd", "/etc/passwd", "a/b"):
        r = client.put(f"/profiles/tara/files/{bad_name}", json={"content": "x"})
        assert r.status_code == 404, f"{bad_name} -> {r.status_code}"

    store = Store(str(tmp_path / "direct"))
    store.create_profile("p", "P", "b", "r")
    for traversal in ("..", "../escaped", "..%2fescaped"):
        with pytest.raises(MalformedRecord):
            store.write_file("p", traversal, "x")
    assert not (tmp_path / "direct" / "profiles" / "escaped").exists()


def test_file_store_size_limit(client):
    from profile_os.storage import Store
    too_big = "x" * (Store.MAX_FILE_BYTES + 1)
    r = client.put("/profiles/tara/files/big.txt", json={"content": too_big})
    assert r.status_code == 422


def test_inbox_flow_between_profiles(client):
    r = client.post("/profiles/tara/messages",
                    json={"to_profile_id": "sidra", "content": "hey, check this out"})
    assert r.status_code == 201, r.text
    msg = r.json()
    assert msg["from_profile_id"] == "tara"
    assert msg["to_profile_id"] == "sidra"
    assert msg["read_at"] is None

    # sender's own inbox is unaffected
    assert client.get("/profiles/tara/inbox").json() == []

    inbox = client.get("/profiles/sidra/inbox").json()
    assert len(inbox) == 1
    assert inbox[0]["content"] == "hey, check this out"

    unread = client.get("/profiles/sidra/inbox", params={"unread_only": True}).json()
    assert len(unread) == 1

    marked = client.post(f"/profiles/sidra/inbox/{msg['id']}/read")
    assert marked.status_code == 200
    assert marked.json()["read_at"] is not None

    still_all = client.get("/profiles/sidra/inbox").json()
    assert len(still_all) == 1
    now_unread = client.get("/profiles/sidra/inbox", params={"unread_only": True}).json()
    assert now_unread == []

    # sidra can't mark a message from its own inbox as read on another profile's behalf
    assert client.post(f"/profiles/tara/inbox/{msg['id']}/read").status_code == 404


def test_send_message_validation(client):
    empty = client.post("/profiles/tara/messages",
                        json={"to_profile_id": "sidra", "content": "   "})
    assert empty.status_code == 422

    unknown_recipient = client.post("/profiles/tara/messages",
                                    json={"to_profile_id": "ghost", "content": "hi"})
    assert unknown_recipient.status_code == 404


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
    record_id = r.json()["id"]

    got = client.get(f"/profiles/tara/stores/hotel_reservations/records/{record_id}",
                     params={"fields": "hotel_name"})
    assert got.status_code == 200 and got.json()["data"] == {"hotel_name": "Grand"}
    updated = client.patch(f"/profiles/tara/stores/hotel_reservations/records/{record_id}",
                           json={"patch": {"notes": "quiet room"}})
    assert updated.status_code == 200 and updated.json()["data"]["notes"] == "quiet room"
    filtered = client.post("/profiles/tara/stores/hotel_reservations/records/query",
                           json={"where": {"city": "Sevilla"}, "fields": ["hotel_name"]})
    assert filtered.status_code == 200 and filtered.json()[0]["data"] == {"hotel_name": "Grand"}

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


def test_demo_page_loads(client):
    r = client.get("/demo")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Unlock settings" in r.text
    assert "Authenticator code" in r.text


def test_delete_profile_removes_everything(client):
    client.post("/profiles/tara/memories",
               json={"kind": "note", "content": "will vanish"})
    r = client.post("/profiles/tara/stores",
                    json={"name": "throwaway", "purpose": "p", "proposed_by": "tara",
                          "schema": {"fields": {"xx": {"type": "string"}}}})
    assert r.status_code == 201

    assert client.delete("/profiles/tara").status_code == 204
    assert client.get("/profiles/tara").status_code == 404
    assert client.get("/profiles").json() and \
        "tara" not in {p["id"] for p in client.get("/profiles").json()}

    assert client.delete("/profiles/ghost").status_code == 404

    # a same-named profile can be recreated cleanly afterward
    r = client.post("/profiles", json={"id": "tara", "display_name": "Tara II"})
    assert r.status_code == 201
    assert client.get("/profiles/tara/stores").json() == []
