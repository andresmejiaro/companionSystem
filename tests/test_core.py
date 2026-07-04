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
    store.closeout("tara", notes="day done", new_state="Logged 3 meals, 1800 kcal.")
    b = store.boot("tara")
    assert b["compact_state"] == "Logged 3 meals, 1800 kcal."
    # closeout also written to inspectable jsonl
    jl = store.profiles_dir / "tara" / "closeouts.jsonl"
    assert "1800 kcal" in jl.read_text()


def test_closeout_requires_new_state(store):
    with pytest.raises(MalformedRecord):
        store.closeout("tara", notes="x", new_state="  ")


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
