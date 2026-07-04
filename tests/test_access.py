import time

import pytest

from profile_os.access import AccessControl, AccessError, verify_secret
from profile_os.storage import Store


@pytest.fixture
def ac(tmp_path):
    s = Store(tmp_path / "data")
    yield AccessControl(s)
    s.close()


def test_create_principals_of_all_kinds(ac):
    for kind in ("human", "app", "bridge", "admin"):
        p = ac.create_principal(kind, f"test {kind}")
        assert p["kind"] == kind and p["disabled_at"] is None


def test_invalid_principal_kind_rejected(ac):
    with pytest.raises(AccessError):
        ac.create_principal("robot", "x")
    with pytest.raises(AccessError):
        ac.create_principal("human", "  ")


def test_invalid_operation_rejected(ac):
    p = ac.create_principal("app", "app")
    with pytest.raises(AccessError):
        ac.grant(p["id"], "fly_to_moon")
    with pytest.raises(AccessError):
        ac.allowed(p["id"], "fly_to_moon")


def test_credential_stores_hash_never_plaintext(ac):
    p = ac.create_principal("bridge", "claude tara bridge")
    c = ac.create_credential(p["id"], "bridge key", "super-secret-token")
    assert "super-secret-token" not in c["secret_hash"]
    assert c["secret_hash"].startswith("pbkdf2$")
    # nothing anywhere in the db contains the plaintext
    dump = "\n".join(ac.db.iterdump())
    assert "super-secret-token" not in dump
    # but the hash verifies
    assert verify_secret("super-secret-token", c["secret_hash"])
    assert not verify_secret("wrong", c["secret_hash"])


def test_revoke_credential(ac):
    p = ac.create_principal("app", "app")
    c = ac.create_credential(p["id"], "k", "s3cret")
    assert c["revoked_at"] is None
    assert ac.revoke_credential(c["id"])["revoked_at"] is not None


def test_profile_grant_scopes_to_that_profile(ac):
    p = ac.create_principal("bridge", "tara bridge")
    ac.grant(p["id"], "boot", profile_id="tara")
    assert ac.allowed(p["id"], "boot", "tara") is True
    assert ac.allowed(p["id"], "boot", "rumbo") is False   # tara grant ≠ rumbo
    assert ac.allowed(p["id"], "remember", "tara") is False  # op not granted
    assert ac.allowed(p["id"], "boot") is False  # profile grant is not global


def test_global_create_profile_grant(ac):
    p = ac.create_principal("human", "owner")
    ac.grant(p["id"], "create_profile")
    assert ac.allowed(p["id"], "create_profile") is True
    # a global grant does not authorize a profile-scoped op of same name space
    assert ac.allowed(p["id"], "boot", "tara") is False


def test_expired_grant_does_not_authorize(ac):
    p = ac.create_principal("bridge", "temp session")
    ac.grant(p["id"], "search", profile_id="tara", expires_at=time.time() - 1)
    assert ac.allowed(p["id"], "search", "tara") is False
    ac.grant(p["id"], "search", profile_id="tara", expires_at=time.time() + 3600)
    assert ac.allowed(p["id"], "search", "tara") is True


def test_revoked_grant_does_not_authorize(ac):
    p = ac.create_principal("app", "health app")
    g = ac.grant(p["id"], "records:write", profile_id="tara")
    assert ac.allowed(p["id"], "records:write", "tara") is True
    ac.revoke_grant(g["id"])
    assert ac.allowed(p["id"], "records:write", "tara") is False


def test_disabled_principal_does_not_authorize(ac):
    p = ac.create_principal("app", "old app")
    ac.grant(p["id"], "boot", profile_id="tara")
    ac.disable_principal(p["id"])
    assert ac.allowed(p["id"], "boot", "tara") is False
    # unknown principal is simply not allowed (no exception from allowed())
    assert ac.allowed("no-such-id", "boot", "tara") is False


def test_all_profiles_wildcard_grant(ac):
    admin = ac.create_principal("admin", "operator")
    ac.grant(admin["id"], "stores:approve", profile_id="*")
    assert ac.allowed(admin["id"], "stores:approve", "tara") is True
    assert ac.allowed(admin["id"], "stores:approve", "sidra") is True
    # wildcard is for profile-scoped checks, not global (None) checks
    assert ac.allowed(admin["id"], "stores:approve") is False


def test_multiple_grants_and_listing(ac):
    p = ac.create_principal("app", "health app")
    ac.grant(p["id"], "boot", profile_id="tara")
    ac.grant(p["id"], "boot", profile_id="rumbo")
    ac.grant(p["id"], "records:read", profile_id="tara")
    assert len(ac.list_grants(p["id"])) == 3
    assert ac.allowed(p["id"], "boot", "rumbo") is True
