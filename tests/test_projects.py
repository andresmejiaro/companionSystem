import pytest

from profile_os import seed
from profile_os.errors import DynStoreConflict
from profile_os.projects import Projects
from profile_os.storage import Store


SCHEMA = {"fields": {
    "title": {"type": "string"},
    "status": {"type": "string"},
    "notes": {"type": "string", "required": False},
}}


@pytest.fixture
def projects(tmp_path):
    store = Store(str(tmp_path / "data"))
    seed.seed(store)
    return Projects(store)


def test_create_and_join_are_pending_until_approved(projects):
    proposed = projects.propose_create("tara", "launch_plan", "Coordinate launch", SCHEMA)
    assert proposed["status"] == "pending"
    assert proposed["members"] == []

    active = projects.approve_create(proposed["id"])
    assert active["status"] == "active"
    assert active["members"][0]["profile_id"] == "tara"
    assert active["members"][0]["role"] == "owner"

    projects.request_join("sidra", active["id"])
    assert not projects.is_member(active["id"], "sidra")
    projects.approve_join("sidra", active["id"])
    assert projects.is_member(active["id"], "sidra")


def test_members_share_schema_validated_records(projects):
    project = projects.propose_create("tara", "launch_plan", "Coordinate launch", SCHEMA)
    projects.approve_create(project["id"])
    projects.approve_join("sidra", project["id"])

    record = projects.add_record("tara", project["id"],
                                 {"title": "Ship", "status": "open"})
    hits = projects.query("sidra", project["id"])
    assert hits[0]["id"] == record["id"]
    assert hits[0]["created_by_profile_id"] == "tara"

    with pytest.raises(DynStoreConflict):
        projects.query("secretary", project["id"])


def test_empty_project_persists_notifies_all_and_admin_can_silence_or_delete(projects):
    project = projects.propose_create("tara", "launch_plan", "Coordinate launch", SCHEMA)
    projects.approve_create(project["id"])
    result = projects.leave("tara", project["id"])
    assert result["empty"] is True
    assert projects.get(project["id"])["members"] == []

    notices = projects.notifications()
    assert len(notices) == 1
    assert notices[0]["kind"] == "empty_project"
    assert all(projects.store.list_inbox(p["id"]) for p in projects.store.list_profiles())

    projects.silence(notices[0]["id"])
    assert projects.notifications() == []
    assert len(projects.notifications(include_silenced=True)) == 1

    assert projects.delete(project["id"])["deleted"] is True
    assert projects.notifications(include_silenced=True) == []


def test_nonempty_project_cannot_be_deleted(projects):
    project = projects.propose_create("tara", "launch_plan", "Coordinate launch", SCHEMA)
    projects.approve_create(project["id"])
    with pytest.raises(DynStoreConflict):
        projects.delete(project["id"])
