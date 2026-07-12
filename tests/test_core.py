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


def test_boot_returns_prompts_state_and_memories(store):
    b = store.boot("sidra")
    assert "Sidra" in b["base_prompt"]
    assert "lane" in b["role_prompt"].lower()
    assert b["compact_state"] == "No active task contract."
    assert any(m["kind"] == "failure_scar" for m in b["recent_memories"])
    assert b["profile"]["allowed_tools"]


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
