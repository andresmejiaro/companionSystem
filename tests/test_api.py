import pytest
from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.storage import Store


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"))
    with TestClient(app) as c:
        yield c


def test_list_and_get_profiles(client):
    ids = {p["id"] for p in client.get("/profiles").json()}
    assert {"sidra", "tara", "tool_probe"} <= ids
    assert client.get("/profiles/tara").json()["display_name"] == "Tara"
    assert client.get("/profiles/ghost").status_code == 404


def test_profile_discovery_metadata_and_startup_routing_guidance(client):
    created = client.post("/profiles", json={
        "id": "travel", "display_name": "Travel", "description": "Plans trips.",
            "signature": "🧭", "who_you_are": "", "what_you_do": "",
    })
    assert created.status_code == 201
    assert created.json()["signature"] == "🧭"

    updated = client.put("/profiles/travel/description", json={"signature": "✈️"})
    assert updated.status_code == 200
    assert updated.json()["description"] == "Plans trips."
    assert updated.json()["signature"] == "✈️"

    booted = client.post("/profiles/travel/boot").json()
    assert "description" not in booted["profile"]

    session = client.post("/profiles/tara/session").json()
    assert session["system_contracts"]["companion"].startswith("# Companion contract")
    assert "search_context" in session["system_contracts"]["companion"]
    assert "search_memories" in session["system_contracts"]["companion"]
    assert "It is not the record-selection path" in session["system_contracts"]["companion"]
    assert "Memories preserve what the companion should keep in mind." in (
        session["system_contracts"]["companion"]
    )
    assert any(item["id"] == "travel" for item in session["companion_directory"])
    assert session["data_sources"] == {
        "profile_stores": [], "joined_projects": [],
    }
    assert session["selection"] == {
        "profile_id": "tara",
        "family_id": "tara",
        "variant_label": "",
        "settled": True,
    }
    assert "Do not ask whether the user meant a sibling" in session["routing_guidance"]

    system = client.post("/profiles", json={
        "id": "system_notifier", "display_name": "System Notifier",
        "profile_kind": "system", "allowed_tools": ["send_message"],
    })
    assert system.status_code == 201
    assert system.json()["profile_kind"] == "system"
    assert client.post("/profiles/system_notifier/session").json()["system_contracts"] == {}

    assert client.post("/profiles", json={
        "id": "too_long", "display_name": "Too long", "description": "x" * 201,
    }).status_code == 422
    assert client.put("/profiles/travel/description", json={"signature": "abcdef"}).status_code == 422


def test_profile_resolver_and_family_aware_session_routing(client):
    assert client.post("/profiles", json={
        "id": "vera",
        "display_name": "Vera",
        "aliases": ["life vera"],
        "family_id": "vera_family",
        "variant_label": "life",
        "is_family_default": True,
    }).status_code == 201
    assert client.post("/profiles", json={
        "id": "dr_vera",
        "display_name": "Dr Vera",
        "aliases": ["doctor vera"],
        "family_id": "vera_family",
        "variant_label": "clinical",
        "is_family_default": False,
    }).status_code == 201

    exact = client.get("/profiles/resolve", params={"q": " VeRa "}).json()
    assert exact["match_basis"] == "exact_id"
    assert exact["resolved_profile_id"] == "vera"

    alias = client.get("/profiles/resolve", params={"q": "Doctor Vera"}).json()
    assert alias["match_basis"] == "alias"
    assert alias["resolved_profile_id"] == "dr_vera"

    family = client.get("/profiles/resolve", params={"q": "vera_family"}).json()
    assert family["match_basis"] == "family_default"
    assert family["resolved_profile_id"] == "vera"

    session = client.post("/profiles/vera/session").json()
    assert session["selection"]["settled"] is True
    assert session["selection"]["profile_id"] == "vera"
    assert "Dr Vera" not in session["routing_guidance"]
    assert any(item["id"] == "dr_vera" for item in session["companion_directory"])

    normalized_session = client.post("/profiles/VERA/session").json()
    assert normalized_session["selection"]["profile_id"] == "vera"


def test_routing_metadata_update_trims_display_name(client):
    response = client.put("/profiles/tara/routing", json={
        "display_name": "  Tara  ",
        "aliases": ["food duck"],
        "variant_label": "bookkeeping",
    })
    assert response.status_code == 200
    assert response.json()["display_name"] == "Tara"
    assert response.json()["aliases"] == ["food duck"]
    assert response.json()["variant_label"] == "bookkeeping"


def test_root_directory_and_admin_shortcuts(client, monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://companions.example.test/")
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/directory"

    directory = client.get("/directory")
    assert directory.status_code == 200
    assert "Companion directory" in directory.text
    assert 'href="/companions/new"' in directory.text
    assert 'href="/settings"' in directory.text
    assert 'href="/question-practice"' in directory.text
    assert 'href="https://rumbo.datacodemath.com/forum/"' in directory.text
    assert "Open the companion forum with its own authenticator check." in directory.text
    assert 'href="https://companions.example.test/session-inspector"' in directory.text
    assert "View the hydrated session packet for a companion." in directory.text

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
    practice = client.get("/question-practice")
    assert practice.status_code == 200
    assert "Unlock LT Rita question practice" in practice.text
    legacy_demo = client.get("/demo", follow_redirects=False)
    assert legacy_demo.status_code == 307
    assert legacy_demo.headers["location"] == "/settings"


def test_boot_endpoint(client):
    r = client.post("/profiles/sidra/boot")
    assert r.status_code == 200
    body = r.json()
    assert body["who_you_are"] and body["compact_state"]


def test_prompt_sections_are_canonical_without_legacy_reads(client):
    boot = client.post("/profiles/sidra/boot")
    assert boot.status_code == 200
    body = boot.json()
    assert list(body)[1:7] == [
        "who_you_are", "signature", "lane", "voice", "what_you_do",
        "how_you_keep_context",
    ]
    assert "base_prompt" not in body and "role_prompt" not in body
    assert {name: body[name] for name in ("signature", "lane", "voice", "how_you_keep_context")} == {
        "signature": "", "lane": "", "voice": "", "how_you_keep_context": "",
    }

    session = client.post("/profiles/sidra/session")
    assert session.status_code == 200
    started = session.json()
    assert "base_prompt" not in started and "role_prompt" not in started
    assert [started[name] for name in ("signature", "lane", "voice", "how_you_keep_context")] == ["", "", "", ""]


def test_prompt_create_uses_canonical_fields_and_rejects_legacy_input_aliases(client):
    created = client.post("/profiles", json={
        "id": "canonical", "display_name": "Canonical",
        "who_you_are": "exact identity", "what_you_do": "exact work",
    })
    assert created.status_code == 201
    booted = client.post("/profiles/canonical/boot").json()
    assert booted["who_you_are"] == "exact identity"
    assert booted["what_you_do"] == "exact work"
    assert [booted[name] for name in ("signature", "lane", "voice", "how_you_keep_context")] == ["", "", "", ""]

    legacy = client.post("/profiles", json={
        "id": "legacy", "display_name": "Legacy",
        "base_prompt": "legacy identity", "role_prompt": "legacy work",
    })
    assert legacy.status_code == 201
    legacy_boot = client.post("/profiles/legacy/boot").json()
    assert legacy_boot["who_you_are"] == legacy_boot["what_you_do"] == ""


def test_session_inspect_matches_start_session_shape_when_auth_is_disabled(client):
    inspected = client.post("/profiles/sidra/session-inspect", json={"totp_code": "123456"})
    assert inspected.status_code == 200
    body = inspected.json()
    assert {"profile", "who_you_are", "signature", "lane", "voice", "what_you_do", "how_you_keep_context", "compact_state", "identity",
            "memories", "recent_exchanges", "you_got_mail", "server_time",
            "system_contracts", "companion_directory", "data_sources"} <= set(body)
    assert body["you_got_mail"] is False
    assert "description" not in body["profile"]
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


def test_context_search_and_prepare_closeout_omit_redundant_sources(client):
    app = client.app
    dyn = app.state.dynstores
    projects = app.state.projects
    schema = {"fields": {"status": {"type": "string"}}}

    dyn.propose("tara", "case_status", "Current case state", "tara", schema)
    dyn.approve("tara", "case_status")
    dyn.add_record("tara", "case_status", {"status": "embargo resolved"})

    project = projects.propose_create("tara", "casework", "Shared case work", schema)
    project = projects.approve_create(project["id"])
    projects.add_record("tara", project["id"], {"status": "embargo resolved"})

    client.post("/profiles/tara/memories", json={
        "kind": "note", "content": "embargo was discussed", "tags": [],
    })
    found = client.get("/profiles/tara/context/search", params={"q": "embargo"})
    assert found.status_code == 200
    assert {item["source_type"] for item in found.json()} == {
        "memory", "profile_store", "shared_project",
    }

    prepared = client.post("/profiles/tara/closeout/prepare")
    assert prepared.status_code == 200
    body = prepared.json()
    assert set(body) == {"profile_id", "instructions"}
    assert body["profile_id"] == "tara"
    assert "data_sources" not in body
    assert body["instructions"] == [
        "Reconcile relevant profile stores and joined shared-project stores; query the owning source when current state matters.",
        "Update existing records for the same real thing and identify duplicates or contradictions; flag conflicts the schema cannot resolve.",
        "Write the companion-appropriate transient, front-of-mind memories.",
        "Complete the existing closeout form.",
        "After doing the thing, use `closeout` to finish the session.",
        "Let the companion close in its own voice.",
    ]

    session = client.post("/profiles/tara/session")
    assert session.status_code == 200
    sources = session.json()["data_sources"]
    assert sources["profile_stores"][0]["name"] == "case_status"
    assert sources["profile_stores"][0]["schema"] == schema
    assert sources["joined_projects"][0]["id"] == project["id"]
    assert sources["joined_projects"][0]["schema"] == schema


def test_context_search_stays_within_the_companion_and_joined_projects(client):
    app = client.app
    dyn = app.state.dynstores
    schema = {"fields": {"status": {"type": "string"}}}
    dyn.propose("sidra", "private_cases", "Private cases", "sidra", schema)
    dyn.approve("sidra", "private_cases")
    dyn.add_record("sidra", "private_cases", {"status": "embargoed"})
    client.post("/profiles/sidra/memories", json={
        "kind": "note", "content": "embargoed private note", "tags": [],
    })

    results = client.get("/profiles/tara/context/search", params={"q": "embargoed"})
    assert results.status_code == 200
    assert results.json() == []


def test_legacy_system_notifier_marker_migrates_without_using_display_name(tmp_path):
    data_dir = tmp_path / "data"
    store = Store(data_dir)
    store.create_profile(
        "legacy_sender", "Operational sender", "You are a non-conversational system identity.",
        "Your sole permitted operation is send_message.",
        allowed_tools=["send_message"],
    )
    store.close()

    migrated = Store(data_dir)
    assert migrated.get_profile("legacy_sender")["profile_kind"] == "system"


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
