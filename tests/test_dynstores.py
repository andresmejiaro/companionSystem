import pytest

from profile_os import seed
from profile_os.dynstores import DynamicStores, validate_schema
from profile_os.errors import DynStoreConflict, DynStoreNotFound, ProfileNotFound, SchemaError
from profile_os.storage import Store

HOTEL_SCHEMA = {"fields": {
    "hotel_name": {"type": "string"},
    "city": {"type": "string"},
    "check_in": {"type": "date"},
    "check_out": {"type": "date"},
    "confirmation_code": {"type": "string", "required": False},
    "notes": {"type": "string", "required": False},
}}

HOTEL_RECORD = {"hotel_name": "Grand Vista", "city": "Sevilla",
                "check_in": "2026-08-01", "check_out": "2026-08-04"}


@pytest.fixture
def dyn(tmp_path):
    s = Store(tmp_path / "data")
    seed.seed(s)
    yield DynamicStores(s)
    s.close()


def _approved(dyn, profile="tara", name="hotel_reservations"):
    dyn.propose(profile, name, "track hotel bookings", profile, HOTEL_SCHEMA)
    return dyn.approve(profile, name)


def test_propose_creates_pending_store(dyn):
    st = dyn.propose("tara", "hotel_reservations", "track hotel bookings",
                     "tara", HOTEL_SCHEMA)
    assert st["status"] == "pending"
    assert st["version"] == 1
    assert st["proposed_by"] == "tara"
    assert st["schema"] == HOTEL_SCHEMA
    assert st["approved_at"] is None


def test_pending_store_rejects_writes_and_queries(dyn):
    dyn.propose("tara", "hotel_reservations", "p", "tara", HOTEL_SCHEMA)
    with pytest.raises(DynStoreConflict):
        dyn.add_record("tara", "hotel_reservations", HOTEL_RECORD)
    with pytest.raises(DynStoreConflict):
        dyn.query_records("tara", "hotel_reservations")


def test_approved_store_accepts_valid_records(dyn):
    _approved(dyn)
    rec = dyn.add_record("tara", "hotel_reservations", HOTEL_RECORD)
    assert rec["schema_version"] == 1
    hits = dyn.query_records("tara", "hotel_reservations", contains="sevilla")
    assert len(hits) == 1 and hits[0]["data"]["hotel_name"] == "Grand Vista"


def test_approved_store_rejects_invalid_records(dyn):
    _approved(dyn)
    with pytest.raises(SchemaError):  # missing required field
        dyn.add_record("tara", "hotel_reservations", {"hotel_name": "X"})
    with pytest.raises(SchemaError):  # unknown field
        dyn.add_record("tara", "hotel_reservations", {**HOTEL_RECORD, "spa": True})
    with pytest.raises(SchemaError):  # wrong type
        dyn.add_record("tara", "hotel_reservations", {**HOTEL_RECORD, "check_in": 20260801})
    with pytest.raises(SchemaError):  # bad date format
        dyn.add_record("tara", "hotel_reservations", {**HOTEL_RECORD, "check_in": "aug 1"})


def test_rejected_store_rejects_writes(dyn):
    dyn.propose("tara", "hotel_reservations", "p", "tara", HOTEL_SCHEMA)
    st = dyn.reject("tara", "hotel_reservations", "too broad")
    assert st["status"] == "rejected" and st["rejection_reason"] == "too broad"
    with pytest.raises(DynStoreConflict):
        dyn.add_record("tara", "hotel_reservations", HOTEL_RECORD)


def test_archived_store_readonly_but_queryable(dyn):
    _approved(dyn)
    dyn.add_record("tara", "hotel_reservations", HOTEL_RECORD)
    st = dyn.archive("tara", "hotel_reservations")
    assert st["status"] == "archived"
    with pytest.raises(DynStoreConflict):
        dyn.add_record("tara", "hotel_reservations", HOTEL_RECORD)
    assert len(dyn.query_records("tara", "hotel_reservations")) == 1


def test_profile_scoping(dyn):
    _approved(dyn, profile="tara")
    # sidra has no such store: cannot write or query tara's data
    with pytest.raises(DynStoreNotFound):
        dyn.add_record("sidra", "hotel_reservations", HOTEL_RECORD)
    with pytest.raises(DynStoreNotFound):
        dyn.query_records("sidra", "hotel_reservations")
    with pytest.raises(ProfileNotFound):
        dyn.propose("ghost", "x_store", "p", "g", HOTEL_SCHEMA)


def test_audit_trail(dyn):
    dyn.propose("tara", "hotel_reservations", "p", "tara", HOTEL_SCHEMA)
    dyn.reject("tara", "hotel_reservations", "no")
    dyn.propose("tara", "hotel_reservations", "p2", "tara", HOTEL_SCHEMA)
    dyn.approve("tara", "hotel_reservations")
    dyn.archive("tara", "hotel_reservations")
    actions = [e["action"] for e in dyn.audit_events("tara", "hotel_reservations")]
    assert actions == ["archived", "approved", "proposed", "rejected", "proposed"]
    assert all(e["actor"] for e in dyn.audit_events("tara"))


def test_versioning_on_reproposal(dyn):
    dyn.propose("tara", "hotel_reservations", "p", "tara", HOTEL_SCHEMA)
    dyn.reject("tara", "hotel_reservations", "no")
    st = dyn.propose("tara", "hotel_reservations", "p2", "tara", HOTEL_SCHEMA)
    assert st["version"] == 2 and st["status"] == "pending"
    # cannot re-propose over a pending or approved definition
    with pytest.raises(DynStoreConflict):
        dyn.propose("tara", "hotel_reservations", "p3", "tara", HOTEL_SCHEMA)
    dyn.approve("tara", "hotel_reservations")
    with pytest.raises(DynStoreConflict):
        dyn.propose("tara", "hotel_reservations", "p3", "tara", HOTEL_SCHEMA)


def test_invalid_schema_fails_clearly(dyn):
    for bad in [
        "not a dict",
        {},
        {"fields": {}},
        {"fields": {"x": {"type": "blob"}}},
        {"fields": {"Bad Name": {"type": "string"}}},
        {"fields": {"x": {"type": "string", "extra": 1}}},
        {"fields": {"x": {"type": "string", "required": "yes"}}},
        {"fields": {"x": {"type": "string"}}, "title": "extra top-level key"},
    ]:
        with pytest.raises(SchemaError):
            validate_schema(bad)
    with pytest.raises(SchemaError):
        dyn.propose("tara", "BAD NAME", "p", "tara", HOTEL_SCHEMA)
    with pytest.raises(SchemaError):
        dyn.propose("tara", "ok_name", "", "tara", HOTEL_SCHEMA)


def test_optional_and_typed_fields(dyn):
    _approved(dyn)
    rec = dyn.add_record("tara", "hotel_reservations",
                         {**HOTEL_RECORD, "confirmation_code": "ABC123"})
    assert rec["data"]["confirmation_code"] == "ABC123"


def test_pending_or_rejected_v2_does_not_hide_v1_records(dyn):
    _approved(dyn)
    dyn.add_record("tara", "hotel_reservations", HOTEL_RECORD)
    dyn.archive("tara", "hotel_reservations")
    # v2 pending must not hide v1's records
    dyn.propose("tara", "hotel_reservations", "v2 schema", "tara", HOTEL_SCHEMA)
    hits = dyn.query_records("tara", "hotel_reservations")
    assert len(hits) == 1 and hits[0]["schema_version"] == 1
    # v2 rejected: still visible
    dyn.reject("tara", "hotel_reservations", "not needed")
    hits = dyn.query_records("tara", "hotel_reservations")
    assert len(hits) == 1 and hits[0]["schema_version"] == 1
    # but no approved version exists, so writes are still rejected
    with pytest.raises(DynStoreConflict):
        dyn.add_record("tara", "hotel_reservations", HOTEL_RECORD)


def test_store_never_approved_still_not_queryable(dyn):
    dyn.propose("tara", "wishlist", "p", "tara", HOTEL_SCHEMA)
    with pytest.raises(DynStoreConflict):
        dyn.query_records("tara", "wishlist")
    dyn.reject("tara", "wishlist", "no")
    with pytest.raises(DynStoreConflict):
        dyn.query_records("tara", "wishlist")


def test_writes_use_latest_approved_version(dyn):
    _approved(dyn)
    dyn.archive("tara", "hotel_reservations")
    dyn.propose("tara", "hotel_reservations", "v2", "tara", HOTEL_SCHEMA)
    dyn.approve("tara", "hotel_reservations")
    rec = dyn.add_record("tara", "hotel_reservations", HOTEL_RECORD)
    assert rec["schema_version"] == 2


def test_date_validation_rejects_impossible_dates(dyn):
    _approved(dyn)
    for bad in ["2026-99-99", "2026-02-30", "aug 1", "2026-13-01", "2026-00-10"]:
        with pytest.raises(SchemaError):
            dyn.add_record("tara", "hotel_reservations", {**HOTEL_RECORD, "check_in": bad})
    rec = dyn.add_record("tara", "hotel_reservations", {**HOTEL_RECORD, "check_in": "2026-08-01"})
    assert rec["data"]["check_in"] == "2026-08-01"
