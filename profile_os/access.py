"""Access-control foundation: principals, credentials, grants.

Storage/service layer — implements the model in ACCESS_CONTROL.md.
Enforcement lives in api.py: every non-public route requires a bearer
credential and a matching grant when PROFILE_OS_AUTH_ENABLED=1.

Design (see ACCESS_CONTROL.md):
- Assistant Profiles are resources, not principals.
- Credentials belong to principals; secrets are stored as salted PBKDF2
  hashes only (stdlib hashlib/secrets, no new dependencies), never plaintext.
- Grants are many-to-many over principal x profile x operation.
  profile_id=None  → global grant (for global operations like create_profile).
  profile_id="*"   → all-profiles grant (admin-style); documented and tested.
- Expired/revoked grants, revoked credentials, and disabled principals never
  authorize.
"""

from __future__ import annotations

import base64
import hashlib
import secrets as _secrets
import time
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

from .storage import Store

ACCESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_principals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                    -- human|app|bridge|admin|agent
    display_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    disabled_at REAL
);
CREATE TABLE IF NOT EXISTS access_credentials (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES access_principals(id),
    label TEXT NOT NULL,
    secret_hash TEXT,                      -- pbkdf2$<iterations>$<salt-hex>$<hash-hex>
    kind TEXT NOT NULL DEFAULT 'secret',   -- secret|ed25519
    public_key TEXT,                       -- base64 raw 32-byte Ed25519 public key
    created_at REAL NOT NULL,
    expires_at REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS access_grants (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES access_principals(id),
    profile_id TEXT,                       -- NULL = global, '*' = all profiles
    operation TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_grants_principal ON access_grants(principal_id, operation);
CREATE TABLE IF NOT EXISTS access_audit (
    id TEXT PRIMARY KEY,
    actor_principal_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS access_invites (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    kind TEXT NOT NULL,                    -- currently only 'agent'
    display_name_hint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    created_by_principal TEXT
);
"""

PRINCIPAL_KINDS = {"human", "app", "bridge", "admin", "agent"}

OPERATIONS = {
    "boot", "remember", "search", "closeout",
    "records:read", "records:write",
    "stores:propose", "stores:approve",
    "create_profile", "delete_profile", "manage_profile",
    "manage_grants", "audit:read", "credentials:manage",
    "identity:read",
}

ALL_PROFILES = "*"
_PBKDF2_ITERATIONS = 100_000


class AccessError(Exception):
    pass


def hash_secret(secret: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else _secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", secret.encode(),
                                     bytes.fromhex(salt_hex), int(iters))
        return _secrets.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class AccessControl:
    """Service over the same SQLite database as Store; api.py enforces per route."""

    def __init__(self, store: Store):
        self._store = store
        self.db.executescript(ACCESS_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Idempotently add columns needed by older databases."""
        cols = {row["name"] for row in
                self.db.execute("PRAGMA table_info(access_credentials)").fetchall()}
        with self.db:
            if "kind" not in cols:
                self.db.execute(
                    "ALTER TABLE access_credentials ADD COLUMN kind TEXT NOT NULL DEFAULT 'secret'")
            if "public_key" not in cols:
                self.db.execute("ALTER TABLE access_credentials ADD COLUMN public_key TEXT")

    @property
    def db(self):
        return self._store.db

    # -- principals -------------------------------------------------------------

    def create_principal(self, kind: str, display_name: str) -> dict:
        if kind not in PRINCIPAL_KINDS:
            raise AccessError(f"invalid principal kind {kind!r};"
                              f" must be one of {sorted(PRINCIPAL_KINDS)}")
        if not display_name or not display_name.strip():
            raise AccessError("display_name is required")
        pid = str(uuid.uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO access_principals (id, kind, display_name, created_at)"
                " VALUES (?,?,?,?)", (pid, kind, display_name, time.time()))
        return self.get_principal(pid)

    def get_principal(self, principal_id: str) -> dict:
        row = self.db.execute("SELECT * FROM access_principals WHERE id=?",
                              (principal_id,)).fetchone()
        if row is None:
            raise AccessError(f"unknown principal {principal_id!r}")
        return dict(row)

    def disable_principal(self, principal_id: str) -> dict:
        self.get_principal(principal_id)
        with self.db:
            self.db.execute(
                "UPDATE access_principals SET disabled_at=? WHERE id=? AND disabled_at IS NULL",
                (time.time(), principal_id))
        return self.get_principal(principal_id)

    # -- credentials --------------------------------------------------------------

    def create_credential(self, principal_id: str, label: str, secret: str,
                          expires_at: float | None = None) -> dict:
        self.get_principal(principal_id)
        if not secret:
            raise AccessError("secret is required")
        cid = str(uuid.uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO access_credentials (id, principal_id, label, secret_hash,"
                " created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (cid, principal_id, label, hash_secret(secret), time.time(), expires_at))
        return self.get_credential(cid)

    def get_credential(self, credential_id: str) -> dict:
        row = self.db.execute("SELECT * FROM access_credentials WHERE id=?",
                              (credential_id,)).fetchone()
        if row is None:
            raise AccessError(f"unknown credential {credential_id!r}")
        return dict(row)

    def revoke_credential(self, credential_id: str) -> dict:
        self.get_credential(credential_id)
        with self.db:
            self.db.execute(
                "UPDATE access_credentials SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (time.time(), credential_id))
        return self.get_credential(credential_id)

    def authenticate_secret(self, secret: str) -> str | None:
        """Resolve a bearer secret to a principal_id, or None.

        Returns None for unknown/revoked/expired credentials and for disabled
        principals. Verification is constant-time per credential; the linear
        scan over active credentials is fine at this scale.
        """
        if not secret:
            return None
        now = time.time()
        rows = self.db.execute(
            "SELECT c.secret_hash, c.principal_id FROM access_credentials c"
            " JOIN access_principals p ON p.id = c.principal_id"
            " WHERE c.revoked_at IS NULL AND (c.expires_at IS NULL OR c.expires_at > ?)"
            " AND p.disabled_at IS NULL", (now,)).fetchall()
        for row in rows:
            if verify_secret(secret, row["secret_hash"]):
                return row["principal_id"]
        return None

    # -- public-key credentials ----------------------------------------------------

    def add_public_key(self, principal_id: str, label: str, public_key_b64: str,
                       expires_at: float | None = None) -> dict:
        self.get_principal(principal_id)
        try:
            raw = base64.b64decode(public_key_b64, validate=True)
            Ed25519PublicKey.from_public_bytes(raw)
        except Exception as e:
            raise AccessError(f"invalid Ed25519 public key: {e}")
        dupe = self.db.execute(
            "SELECT 1 FROM access_credentials WHERE kind='ed25519' AND public_key=?"
            " AND revoked_at IS NULL", (public_key_b64,)).fetchone()
        if dupe is not None:
            raise AccessError("public key already registered")
        cid = str(uuid.uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO access_credentials (id, principal_id, label, kind,"
                " public_key, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                (cid, principal_id, label, "ed25519", public_key_b64, time.time(),
                 expires_at))
        return self.get_credential(cid)

    def authenticate_signature(self, key_id: str, message: bytes,
                               signature: bytes) -> dict | None:
        """Resolve an Ed25519-signed message to a principal dict, or None.

        Same "never authorize when disabled/revoked/expired" semantics as
        authenticate_secret().
        """
        now = time.time()
        row = self.db.execute(
            "SELECT c.public_key, c.principal_id FROM access_credentials c"
            " JOIN access_principals p ON p.id = c.principal_id"
            " WHERE c.id=? AND c.kind='ed25519' AND c.revoked_at IS NULL"
            " AND (c.expires_at IS NULL OR c.expires_at > ?) AND p.disabled_at IS NULL",
            (key_id, now)).fetchone()
        if row is None or row["public_key"] is None:
            return None
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(row["public_key"]))
            public_key.verify(signature, message)
        except (InvalidSignature, ValueError, TypeError):
            return None
        return self.get_principal(row["principal_id"])

    # -- audit ----------------------------------------------------------------------

    def record_audit(self, actor_principal_id: str, action: str, detail: str = "") -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO access_audit (id, actor_principal_id, action, detail,"
                " created_at) VALUES (?,?,?,?,?)",
                (str(uuid.uuid4()), actor_principal_id, action, detail, time.time()))

    # -- grants ---------------------------------------------------------------------

    def grant(self, principal_id: str, operation: str,
              profile_id: str | None = None, expires_at: float | None = None) -> dict:
        self.get_principal(principal_id)
        if operation not in OPERATIONS:
            raise AccessError(f"invalid operation {operation!r};"
                              f" must be one of {sorted(OPERATIONS)}")
        gid = str(uuid.uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO access_grants (id, principal_id, profile_id, operation,"
                " created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (gid, principal_id, profile_id, operation, time.time(), expires_at))
        return dict(self.db.execute("SELECT * FROM access_grants WHERE id=?",
                                    (gid,)).fetchone())

    def revoke_grant(self, grant_id: str) -> dict:
        row = self.db.execute("SELECT * FROM access_grants WHERE id=?",
                              (grant_id,)).fetchone()
        if row is None:
            raise AccessError(f"unknown grant {grant_id!r}")
        with self.db:
            self.db.execute(
                "UPDATE access_grants SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (time.time(), grant_id))
        return dict(self.db.execute("SELECT * FROM access_grants WHERE id=?",
                                    (grant_id,)).fetchone())

    def list_grants(self, principal_id: str) -> list[dict]:
        self.get_principal(principal_id)
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM access_grants WHERE principal_id=? ORDER BY created_at",
            (principal_id,)).fetchall()]

    def visible_profile_ids(self, principal_id: str) -> set[str] | None:
        """Profile ids the principal holds any active grant on.

        Returns None to mean "all profiles" when the principal has an active
        wildcard ('*') grant. Unknown/disabled principals see nothing.
        """
        row = self.db.execute("SELECT disabled_at FROM access_principals WHERE id=?",
                              (principal_id,)).fetchone()
        if row is None or row["disabled_at"] is not None:
            return set()
        rows = self.db.execute(
            "SELECT DISTINCT profile_id FROM access_grants WHERE principal_id=?"
            " AND profile_id IS NOT NULL AND revoked_at IS NULL"
            " AND (expires_at IS NULL OR expires_at > ?)",
            (principal_id, time.time())).fetchall()
        ids = {r["profile_id"] for r in rows}
        return None if ALL_PROFILES in ids else ids

    # -- authorization check ------------------------------------------------------------

    def allowed(self, principal_id: str, operation: str,
                profile_id: str | None = None) -> bool:
        """True iff an active grant authorizes (principal, operation, profile).

        Matching: a grant applies if its profile_id equals the requested
        profile_id, or the grant is for ALL_PROFILES ('*') and a profile was
        requested, or both are None (global operation). Grants must be
        unrevoked and unexpired; the principal must exist and be enabled.
        """
        if operation not in OPERATIONS:
            raise AccessError(f"invalid operation {operation!r}")
        row = self.db.execute("SELECT disabled_at FROM access_principals WHERE id=?",
                              (principal_id,)).fetchone()
        if row is None or row["disabled_at"] is not None:
            return False
        now = time.time()
        if profile_id is None:
            profile_clause = "profile_id IS NULL"
            params = [principal_id, operation, now]
        else:
            profile_clause = "profile_id IN (?, ?)"
            params = [principal_id, operation, now, profile_id, ALL_PROFILES]
        hit = self.db.execute(
            f"SELECT 1 FROM access_grants WHERE principal_id=? AND operation=?"
            f" AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)"
            f" AND {profile_clause} LIMIT 1", params).fetchone()
        return hit is not None
