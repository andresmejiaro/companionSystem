"""Device registry and authorization for the Tamagochi API."""

from __future__ import annotations

import secrets
import time
import uuid

from .access import AccessControl, AccessError, hash_secret, verify_secret

PET_PROFILES = ("hilo", "limo")
PET_OPERATIONS = ("pet:converse", "boot", "search", "records:read")
PET_AUTHORIZABLE_OPERATIONS = (*PET_OPERATIONS, "identity:read")

PET_DEVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pet_devices (
    device_id TEXT NOT NULL UNIQUE,
    credential_id TEXT PRIMARY KEY REFERENCES access_credentials(id),
    principal_id TEXT NOT NULL REFERENCES access_principals(id),
    device_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    revoked_at REAL
);
"""


class PetDeviceService:
    def __init__(self, access: AccessControl):
        self.access = access
        access.db.executescript(PET_DEVICE_SCHEMA)

    @property
    def db(self):
        # Store intentionally keeps one SQLite connection per calling thread.
        return self.access.db

    def issue(self, profile_ids: list[str], expires_in: int = 3600,
              device_name: str = "Tamagochi device", identity_read: bool = False) -> dict:
        """Create a least-privilege device and return its secret exactly once."""
        profiles = list(dict.fromkeys(profile_ids))
        if not profiles or any(p not in PET_PROFILES for p in profiles):
            raise ValueError("profiles must be selected from hilo,limo")
        if not 1 <= expires_in <= 86400:
            raise ValueError("expiry must be between 1 and 86400 seconds")
        # Validate against the existing profile registry before creating any rows.
        existing = {r["id"] for r in self.db.execute(
            "SELECT id FROM profiles WHERE profile_kind='companion'").fetchall()}
        missing = sorted(set(profiles) - existing)
        if missing:
            raise ValueError(f"canonical companion profile(s) missing: {', '.join(missing)}")
        secret = secrets.token_urlsafe(32)
        principal_id = str(uuid.uuid4())
        credential_id = str(uuid.uuid4())
        device_id = str(uuid.uuid4())
        created_at = time.time()
        expires_at = time.time() + expires_in
        with self.db:
            self.db.execute(
                "INSERT INTO access_principals (id,kind,display_name,created_at)"
                " VALUES (?,'app',?,?)",
                (principal_id, f"Tamagochi device: {device_name}", created_at))
            self.db.execute(
                "INSERT INTO access_credentials (id,principal_id,label,secret_hash,kind,created_at,expires_at)"
                " VALUES (?,?,?,?,'secret',?,?)",
                (credential_id, principal_id, f"pet device {device_name}",
                 hash_secret(secret), created_at, expires_at))
            self.db.execute(
                "INSERT INTO pet_devices (device_id, credential_id, principal_id, device_name, created_at)"
                " VALUES (?,?,?,?,?)",
                (device_id, credential_id, principal_id, device_name, created_at))
            for profile_id in profiles:
                for operation in PET_OPERATIONS:
                    self.db.execute(
                        "INSERT INTO access_grants (id,principal_id,profile_id,operation,created_at,expires_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (str(uuid.uuid4()), principal_id, profile_id, operation,
                         created_at, expires_at))
            if identity_read:
                for profile_id in profiles:
                    self.db.execute(
                        "INSERT INTO access_grants (id,principal_id,profile_id,operation,created_at,expires_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (str(uuid.uuid4()), principal_id, profile_id, "identity:read",
                         created_at, expires_at))
        return {"credential_id": credential_id, "principal_id": principal_id,
                "device_id": device_id, "expires_at": expires_at, "secret": secret,
                "profiles": profiles}

    def revoke(self, credential_id: str) -> None:
        row = self.db.execute(
            "SELECT credential_id FROM pet_devices WHERE credential_id=? AND revoked_at IS NULL",
            (credential_id,)).fetchone()
        if row is None:
            raise AccessError("unknown or revoked pet device credential")
        with self.db:
            now = time.time()
            self.db.execute(
                "UPDATE access_credentials SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (now, credential_id))
            self.db.execute("UPDATE pet_devices SET revoked_at=? WHERE credential_id=?",
                            (now, credential_id))

    def authenticate(self, bearer: str) -> dict | None:
        """Resolve a currently valid registered device credential, never a bridge key."""
        if not bearer:
            return None
        now = time.time()
        rows = self.db.execute(
            "SELECT c.id AS credential_id,c.principal_id,c.secret_hash,c.expires_at,c.revoked_at,"
            "p.disabled_at,d.revoked_at AS device_revoked FROM access_credentials c "
            "JOIN access_principals p ON p.id=c.principal_id "
            "JOIN pet_devices d ON d.credential_id=c.id AND d.principal_id=c.principal_id "
            "WHERE c.kind='secret' AND c.revoked_at IS NULL AND d.revoked_at IS NULL "
            "AND p.disabled_at IS NULL AND (c.expires_at IS NULL OR c.expires_at>?)",
            (now,)).fetchall()
        for row in rows:
            if verify_secret(bearer, row["secret_hash"]):
                return {"credential_id": row["credential_id"],
                        "principal_id": row["principal_id"]}
        return None

    def authorize(self, principal_id: str, profile_id: str,
                  operation: str, credential_id: str) -> bool:
        if (not credential_id or profile_id not in PET_PROFILES or
                operation not in PET_AUTHORIZABLE_OPERATIONS):
            return False
        principal = self.db.execute(
            "SELECT disabled_at FROM access_principals WHERE id=?", (principal_id,)).fetchone()
        if principal is None or principal["disabled_at"] is not None:
            return False
        row = self.db.execute(
            "SELECT 1 FROM pet_devices d JOIN access_credentials c ON c.id=d.credential_id "
            "WHERE d.principal_id=? AND d.credential_id=? AND c.principal_id=d.principal_id "
            "AND d.revoked_at IS NULL AND c.revoked_at IS NULL "
            "AND (c.expires_at IS NULL OR c.expires_at>?) LIMIT 1",
            (principal_id, credential_id, time.time())).fetchone()
        return bool(row and self.access.allowed(principal_id, operation, profile_id))

    def granted_profiles(self, principal_id: str, credential_id: str | None = None) -> list[str]:
        required = PET_OPERATIONS
        return [profile for profile in PET_PROFILES
                if all(self.authorize(principal_id, profile, op, credential_id)
                       for op in required)]

