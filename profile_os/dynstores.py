"""Dynamic profile data stores: propose → approve/reject → write/query → archive.

Core principle: a profile PROPOSES a durable structure, the user/admin
APPROVES it, the backend ENFORCES it. The platform never hardcodes what any
profile may store. Every lifecycle change is written to an audit table.

Schema format (deliberately tiny, validated locally, no dependencies):

    {"fields": {
        "hotel_name": {"type": "string"},
        "nights":     {"type": "integer"},
        "notes":      {"type": "string", "required": false}
    }}

Types: string | number | integer | boolean | date (date = "YYYY-MM-DD" string).
Fields are required unless "required": false. Unknown fields in a record are
rejected. This subset covers slice-two needs; JSON Schema can replace it
behind validate_record() later if it ever falls short.

Versioning rule (slice two): schemas are immutable once proposed. To change a
schema, archive (or after rejection) re-propose the same store name — the new
proposal gets version = latest + 1 and its own approval cycle. Records are
keyed by store *name*, so records written under old versions stay queryable.
No data migrations are performed.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import date

from .errors import DynStoreConflict, DynStoreNotFound, SchemaError
from .storage import Store

DYN_SCHEMA = """
CREATE TABLE IF NOT EXISTS dynamic_stores (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id),
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    schema TEXT NOT NULL,                  -- JSON, see module docstring
    status TEXT NOT NULL DEFAULT 'pending',-- pending|approved|rejected|archived
    rejection_reason TEXT,
    created_at REAL NOT NULL,
    approved_at REAL,
    rejected_at REAL,
    UNIQUE (profile_id, name, version)
);
CREATE TABLE IF NOT EXISTS dynamic_records (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id),
    store_name TEXT NOT NULL,
    schema_version INTEGER NOT NULL,       -- version the record was validated against
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dynrec ON dynamic_records(profile_id, store_name);
CREATE TABLE IF NOT EXISTS store_audit (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    store_name TEXT NOT NULL,
    action TEXT NOT NULL,                  -- proposed|approved|rejected|archived
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""

FIELD_TYPES = {"string", "number", "integer", "boolean", "date"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_schema(schema: dict) -> None:
    if not isinstance(schema, dict) or set(schema) != {"fields"}:
        raise SchemaError('schema must be an object with exactly one key: "fields"')
    fields = schema["fields"]
    if not isinstance(fields, dict) or not fields:
        raise SchemaError("schema.fields must be a non-empty object")
    for fname, spec in fields.items():
        if not NAME_RE.match(fname):
            raise SchemaError(f"invalid field name {fname!r} (lowercase slug required)")
        if not isinstance(spec, dict) or not set(spec) <= {"type", "required"}:
            raise SchemaError(f"field {fname!r}: spec keys are 'type' and optional 'required'")
        if spec.get("type") not in FIELD_TYPES:
            raise SchemaError(f"field {fname!r}: type must be one of {sorted(FIELD_TYPES)}")
        if not isinstance(spec.get("required", True), bool):
            raise SchemaError(f"field {fname!r}: 'required' must be a boolean")


def _valid_date(value: str) -> bool:
    """A real calendar date in YYYY-MM-DD form (regex alone lets 2026-02-30 through)."""
    if not DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def validate_record(schema: dict, data: dict) -> None:
    if not isinstance(data, dict) or not data:
        raise SchemaError("record data must be a non-empty object")
    fields = schema["fields"]
    unknown = set(data) - set(fields)
    if unknown:
        raise SchemaError(f"unknown fields: {sorted(unknown)}")
    for fname, spec in fields.items():
        if fname not in data:
            if spec.get("required", True):
                raise SchemaError(f"missing required field {fname!r}")
            continue
        value, ftype = data[fname], spec["type"]
        ok = (
            (ftype == "string" and isinstance(value, str))
            or (ftype == "boolean" and isinstance(value, bool))
            or (ftype == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (ftype == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
            or (ftype == "date" and isinstance(value, str) and _valid_date(value))
        )
        if not ok:
            raise SchemaError(f"field {fname!r}: expected {ftype}, got {value!r}")


class DynamicStores:
    """Service over the same SQLite database as Store. Profile-scoped throughout."""

    def __init__(self, store: Store):
        self._store = store
        self.db.executescript(DYN_SCHEMA)

    @property
    def db(self):
        return self._store.db

    # -- lifecycle -------------------------------------------------------------

    def propose(self, profile_id: str, name: str, purpose: str,
                proposed_by: str, schema: dict) -> dict:
        self._store._require_profile(profile_id)
        if not NAME_RE.match(name or ""):
            raise SchemaError("store name must be a lowercase slug (a-z, 0-9, _)")
        if not purpose or not purpose.strip():
            raise SchemaError("purpose is required")
        if not proposed_by or not proposed_by.strip():
            raise SchemaError("proposed_by is required")
        validate_schema(schema)
        latest = self._latest(profile_id, name)
        if latest and latest["status"] in ("pending", "approved"):
            raise DynStoreConflict(
                f"store {name!r} already has a {latest['status']} definition "
                f"(v{latest['version']}); archive or reject it before re-proposing")
        version = (latest["version"] + 1) if latest else 1
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO dynamic_stores (id, profile_id, name, version, purpose,"
                " proposed_by, schema, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), profile_id, name, version, purpose,
                 proposed_by, json.dumps(schema), "pending", now))
        self._audit(profile_id, name, "proposed", proposed_by, f"v{version}: {purpose}")
        return self.get(profile_id, name)

    def approve(self, profile_id: str, name: str, actor: str = "admin") -> dict:
        row = self._require(profile_id, name)
        if row["status"] != "pending":
            raise DynStoreConflict(f"only pending stores can be approved (is {row['status']})")
        with self.db:
            self.db.execute(
                "UPDATE dynamic_stores SET status='approved', approved_at=? WHERE id=?",
                (time.time(), row["id"]))
        self._audit(profile_id, name, "approved", actor, f"v{row['version']}")
        return self.get(profile_id, name)

    def reject(self, profile_id: str, name: str, reason: str, actor: str = "admin") -> dict:
        row = self._require(profile_id, name)
        if row["status"] != "pending":
            raise DynStoreConflict(f"only pending stores can be rejected (is {row['status']})")
        if not reason or not reason.strip():
            raise SchemaError("rejection reason is required")
        with self.db:
            self.db.execute(
                "UPDATE dynamic_stores SET status='rejected', rejected_at=?,"
                " rejection_reason=? WHERE id=?", (time.time(), reason, row["id"]))
        self._audit(profile_id, name, "rejected", actor, f"v{row['version']}: {reason}")
        return self.get(profile_id, name)

    def archive(self, profile_id: str, name: str, actor: str = "admin") -> dict:
        row = self._require(profile_id, name)
        if row["status"] != "approved":
            raise DynStoreConflict(f"only approved stores can be archived (is {row['status']})")
        with self.db:
            self.db.execute("UPDATE dynamic_stores SET status='archived' WHERE id=?",
                            (row["id"],))
        self._audit(profile_id, name, "archived", actor, f"v{row['version']}")
        return self.get(profile_id, name)

    # -- records ---------------------------------------------------------------

    def add_record(self, profile_id: str, name: str, data: dict) -> dict:
        latest = self._require(profile_id, name)
        # Writes go to the latest APPROVED version, even if a newer version
        # is pending or rejected.
        row = self._latest_with_status(profile_id, name, ("approved",))
        if row is None:
            raise DynStoreConflict(
                f"store {name!r} has no approved version (latest is"
                f" v{latest['version']}, {latest['status']}); writes rejected")
        schema = json.loads(row["schema"])
        validate_record(schema, data)
        rid, now = str(uuid.uuid4()), time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO dynamic_records (id, profile_id, store_name, schema_version,"
                " data, created_at) VALUES (?,?,?,?,?,?)",
                (rid, profile_id, name, row["version"], json.dumps(data), now))
        return {"id": rid, "store": name, "schema_version": row["version"],
                "data": data, "created_at": now}

    def query_records(self, profile_id: str, name: str,
                      contains: str | None = None, limit: int = 50) -> list[dict]:
        latest = self._require(profile_id, name)
        # Queryable if ANY version was ever approved or archived — a pending or
        # rejected newer version must not hide existing records.
        if self._latest_with_status(profile_id, name, ("approved", "archived")) is None:
            raise DynStoreConflict(f"store {name!r} is {latest['status']}; not queryable")
        sql = "SELECT * FROM dynamic_records WHERE profile_id=? AND store_name=?"
        params: list = [profile_id, name]
        if contains:
            sql += " AND data LIKE ? COLLATE NOCASE"
            params.append(f"%{contains}%")
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [{"id": r["id"], "store": r["store_name"],
                 "schema_version": r["schema_version"],
                 "data": json.loads(r["data"]), "created_at": r["created_at"]}
                for r in self.db.execute(sql, params).fetchall()]

    # -- introspection -----------------------------------------------------------

    def get(self, profile_id: str, name: str) -> dict:
        return self._to_dict(self._require(profile_id, name))

    def list(self, profile_id: str) -> list[dict]:
        self._store._require_profile(profile_id)
        rows = self.db.execute(
            "SELECT * FROM dynamic_stores WHERE profile_id=?"
            " ORDER BY name, version DESC", (profile_id,)).fetchall()
        latest, seen = [], set()
        for r in rows:  # latest version per name
            if r["name"] not in seen:
                seen.add(r["name"])
                latest.append(self._to_dict(r))
        return latest

    def audit_events(self, profile_id: str, name: str | None = None,
                     limit: int = 100) -> list[dict]:
        self._store._require_profile(profile_id)
        sql = "SELECT * FROM store_audit WHERE profile_id=?"
        params: list = [profile_id]
        if name:
            sql += " AND store_name=?"
            params.append(name)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def delete_profile_data(self, profile_id: str) -> None:
        """Drop all dynamic-store rows for a deleted profile. Storage.delete_profile
        handles the core profile/memory rows; this covers the dynstores tables."""
        with self.db:
            self.db.execute("DELETE FROM dynamic_records WHERE profile_id=?", (profile_id,))
            self.db.execute("DELETE FROM dynamic_stores WHERE profile_id=?", (profile_id,))
            self.db.execute("DELETE FROM store_audit WHERE profile_id=?", (profile_id,))

    # -- internals ---------------------------------------------------------------

    def _latest(self, profile_id: str, name: str):
        return self.db.execute(
            "SELECT * FROM dynamic_stores WHERE profile_id=? AND name=?"
            " ORDER BY version DESC LIMIT 1", (profile_id, name)).fetchone()

    def _latest_with_status(self, profile_id: str, name: str, statuses: tuple):
        marks = ",".join("?" for _ in statuses)
        return self.db.execute(
            f"SELECT * FROM dynamic_stores WHERE profile_id=? AND name=?"
            f" AND status IN ({marks}) ORDER BY version DESC LIMIT 1",
            (profile_id, name, *statuses)).fetchone()

    def _require(self, profile_id: str, name: str):
        self._store._require_profile(profile_id)
        row = self._latest(profile_id, name)
        if row is None:
            raise DynStoreNotFound(profile_id, name)
        return row

    def _audit(self, profile_id: str, name: str, action: str, actor: str, detail: str):
        with self.db:
            self.db.execute(
                "INSERT INTO store_audit (id, profile_id, store_name, action, actor,"
                " detail, created_at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), profile_id, name, action, actor, detail, time.time()))

    @staticmethod
    def _to_dict(row) -> dict:
        d = dict(row)
        d["schema"] = json.loads(d["schema"])
        return d
