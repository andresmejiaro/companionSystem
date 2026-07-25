import pytest

from profile_os import seed
from profile_os.adapters import FakeModelAdapter
from profile_os.errors import MalformedMemoryEvent, MalformedRecord, ProfileNotFound
from profile_os.storage import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    seed.seed(s)
    yield s
    s.close()


def test_profiles_seeded(store):
    ids = {p["id"] for p in store.list_profiles()}
    assert {"sidra", "tara"} <= ids


def test_profile_resolution_precedence_and_family_default(store):
    store.create_profile(
        "vera", "Vera", "", "", aliases=["life vera"],
        family_id="vera_family", variant_label="life",
        is_family_default=True,
    )
    store.create_profile(
        "dr_vera", "Dr Vera", "", "", aliases=["doctor vera"],
        family_id="vera_family", variant_label="clinical",
        is_family_default=False,
    )

    exact = store.resolve_profile("  VeRa  ")
    assert exact["status"] == "resolved"
    assert exact["match_basis"] == "exact_id"
    assert exact["resolved_profile_id"] == "vera"

    alias = store.resolve_profile("DOCTOR   VERA")
    assert alias["match_basis"] == "alias"
    assert alias["resolved_profile_id"] == "dr_vera"

    display = store.resolve_profile("Dr Vera")
    assert display["match_basis"] == "display_name"
    assert display["resolved_profile_id"] == "dr_vera"

    family = store.resolve_profile("vera_family")
    assert family["match_basis"] == "family_default"
    assert family["resolved_profile_id"] == "vera"

    missing = store.resolve_profile("not-a-companion")
    assert missing["status"] == "not_found"
    assert missing["candidates"] == []


def test_profile_resolution_reports_same_tier_collisions(store):
    store.create_profile("one", "Shared", "", "", aliases=["same"])
    store.create_profile("two", "Shared", "", "", aliases=["same"])

    alias = store.resolve_profile("same")
    assert alias["status"] == "ambiguous"
    assert alias["match_basis"] == "alias"
    assert {p["id"] for p in alias["candidates"]} == {"one", "two"}

    display = store.resolve_profile("shared")
    assert display["status"] == "ambiguous"
    assert display["match_basis"] == "display_name"


def test_routing_metadata_trims_names_and_keeps_one_family_default(store):
    store.create_profile("base", " Base  ", "", "", family_id="birds")
    store.create_profile(
        "variant", "Variant", "", "", family_id="birds",
        is_family_default=False,
    )
    assert store.get_profile("base")["display_name"] == "Base"

    updated = store.update_routing_metadata(
        "variant",
        display_name="  Variant GM  ",
        aliases=[" GM ", "gm"],
        is_family_default=True,
    )
    assert updated["display_name"] == "Variant GM"
    assert updated["aliases"] == ["GM"]
    assert updated["is_family_default"] is True
    assert store.get_profile("base")["is_family_default"] is False


def test_boot_returns_prompts_state_and_memories(store):
    b = store.boot("sidra")
    assert list(b)[1:7] == [
        "who_you_are", "signature", "lane", "voice", "what_you_do",
        "how_you_keep_context",
    ]
    assert "Sidra" in b["who_you_are"]
    assert "lane" in b["what_you_do"].lower()
    assert b["signature"] == b["lane"] == b["voice"] == ""
    assert b["how_you_keep_context"] == ""
    # Legacy reads remain aliases, not separate persisted prompt bodies.
    assert "Sidra" in b["base_prompt"]
    assert "lane" in b["role_prompt"].lower()
    assert b["compact_state"] == "No active task contract."
    assert any(m["kind"] == "failure_scar" for m in b["recent_memories"])
    assert b["profile"]["allowed_tools"]


def test_legacy_prompt_files_are_renamed_byte_for_byte_and_new_sections_are_empty(store):
    profile_dir = store.profiles_dir / "sidra"
    before_who = (profile_dir / "who_you_are.md").read_bytes()
    before_what = (profile_dir / "what_you_do.md").read_bytes()
    (profile_dir / "who_you_are.md").replace(profile_dir / "base_prompt.md")
    (profile_dir / "what_you_do.md").replace(profile_dir / "role_prompt.md")
    for name in ("signature.md", "lane.md", "voice.md", "how_you_keep_context.md"):
        (profile_dir / name).unlink()

    store.close()
    reopened = Store(store.data_dir)
    try:
        assert (profile_dir / "who_you_are.md").read_bytes() == before_who
        assert (profile_dir / "what_you_do.md").read_bytes() == before_what
        assert not (profile_dir / "base_prompt.md").exists()
        assert not (profile_dir / "role_prompt.md").exists()
        booted = reopened.boot("sidra")
        assert [booted[name] for name in ("signature", "lane", "voice", "how_you_keep_context")] == ["", "", "", ""]
        assert booted["base_prompt"] == booted["who_you_are"]
        assert booted["role_prompt"] == booted["what_you_do"]
    finally:
        reopened.close()


def test_boot_unknown_profile_fails_clearly(store):
    with pytest.raises(ProfileNotFound) as e:
        store.boot("nope")
    assert "nope" in str(e.value)


def test_remember_and_search(store):
    ev = store.remember("sidra", {"kind": "decision",
                                  "content": "Use SQLite for slice zero",
                                  "tags": ["storage"]})
    assert ev["id"]
    hits = store.search("sidra", "sqlite")
    assert any(h["id"] == ev["id"] for h in hits)
    # tag search
    assert store.search("sidra", "storage")
    # scoped: tara doesn't see sidra memories
    assert not store.search("tara", "sqlite")


def test_malformed_memory_events_fail_clearly(store):
    with pytest.raises(MalformedMemoryEvent):
        store.remember("sidra", {"kind": "not_a_kind", "content": "x"})
    with pytest.raises(MalformedMemoryEvent):
        store.remember("sidra", {"kind": "note", "content": "   "})
    with pytest.raises(MalformedMemoryEvent):
        store.remember("sidra", {"kind": "note", "content": "ok", "tags": [1]})
    with pytest.raises(MalformedMemoryEvent):
        store.remember("sidra", "not a dict")


def test_closeout_updates_compact_state(store):
    store.closeout("tara", facts="Logged 3 meals, 1800 kcal.", texture="Calm.", exchange="User: done.\nTara: Logged.", notes="day done")
    b = store.boot("tara")
    assert "## Facts\nLogged 3 meals, 1800 kcal." in b["compact_state"]
    assert "## Meaningful exchange\nUser: done." in b["compact_state"]
    # closeout also written to inspectable jsonl
    jl = store.profiles_dir / "tara" / "closeouts.jsonl"
    assert "1800 kcal" in jl.read_text()


def test_closeout_requires_new_state(store):
    with pytest.raises(MalformedRecord):
        store.closeout("tara", facts="  ", texture="x", exchange="x")


def test_tara_domain_data_queryable(store):
    products = store.query_domain("tara", "products")
    assert len(products) == 2
    hits = store.query_domain("tara", "products", contains="yogurt")
    assert len(hits) == 1 and hits[0]["data"]["calibrated"] is True
    assert "meals" in store.list_domain_stores("tara")


def test_domain_record_validation(store):
    with pytest.raises(MalformedRecord):
        store.add_domain_record("tara", "products", {})
    with pytest.raises(ProfileNotFound):
        store.add_domain_record("ghost", "products", {"a": 1})


def test_fake_model_adapter_is_deterministic():
    a = FakeModelAdapter()
    out1 = a.run("sys", "hello")
    out2 = a.run("sys", "hello")
    assert out1 == out2 and "hello" in out1


def test_boot_respects_max_boot_events(store):
    store.create_profile("mini", "Mini", "base", "role",
                         memory_policy={"max_boot_events": 2})
    for i in range(5):
        store.remember("mini", {"kind": "note", "content": f"event {i}"})
    b = store.boot("mini")
    assert len(b["recent_memories"]) == 2
    assert b["recent_memories"][0]["content"] == "event 4"  # newest first
    # explicit argument overrides the policy
    assert len(store.boot("mini", recent_events=5)["recent_memories"]) == 5


def test_boot_invalid_policy_uses_safe_default(store):
    store.create_profile("badpol", "BadPol", "base", "role",
                         memory_policy={"max_boot_events": "lots"})
    for i in range(12):
        store.remember("badpol", {"kind": "note", "content": f"e{i}"})
    assert len(store.boot("badpol")["recent_memories"]) == 10
    # bool True must not count as a valid int
    store.create_profile("boolpol", "BoolPol", "base", "role",
                         memory_policy={"max_boot_events": True})
    store.remember("boolpol", {"kind": "note", "content": "x"})
    assert len(store.boot("boolpol")["recent_memories"]) == 1  # default, not 1-as-True


def test_foreign_keys_enforced(store):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        with store.db:
            store.db.execute(
                "INSERT INTO memory_events (id, profile_id, kind, content, tags, created_at)"
                " VALUES ('x', 'ghost', 'note', 'c', '[]', 0)")


def test_store_usable_from_multiple_threads(store):
    import threading
    errors = []

    def worker(i):
        try:
            store.remember("sidra", {"kind": "note", "content": f"thread {i}"})
            store.boot("sidra")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(store.search("sidra", "thread")) == 8


def test_create_profile_rolls_back_on_file_write_failure(store, monkeypatch):
    from pathlib import Path

    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        store.create_profile("doomed", "Doomed", "base", "role")
    monkeypatch.undo()
    assert "doomed" not in {p["id"] for p in store.list_profiles()}
    assert not (store.profiles_dir / "doomed").exists()
    # id is reusable after the failed attempt
    store.create_profile("doomed", "Doomed", "base", "role")
    assert store.boot("doomed")["base_prompt"] == "base"
