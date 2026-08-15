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

Types: string | number | integer | boolean | date | string_list | object |
object_list (date = "YYYY-MM-DD" string).
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
import math
import random
import re
import sqlite3
import time
import uuid
from datetime import date, datetime

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
    created_at REAL NOT NULL,
    updated_at REAL
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

FIELD_TYPES = {"string", "number", "integer", "boolean", "date",
               "string_list", "object", "object_list"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

THREAD_CONTINUITY_STORE = "thread_continuity"
THREAD_CONTINUITY_PURPOSE = (
    "Per-companion continuity for The Thread: positions, stakes, and open loops. "
    "Exact forum posts remain owned by The Thread."
)
THREAD_CONTINUITY_SCHEMA = {
    "fields": {
        "discussion_id": {"type": "string"},
        "source_type": {"type": "string"},
        "source_id": {"type": "string"},
        "subject": {"type": "string"},
        "position": {"type": "string"},
        "stake": {"type": "string", "required": False},
        "open_loop": {"type": "string", "required": False},
        "status": {"type": "string"},
        "occurred_at": {"type": "string"},
        "updated_at": {"type": "string"},
    }
}
THREAD_SOURCE_TYPES = {"post", "reply", "vote", "reaction", "proposal"}
THREAD_STATUSES = {"active", "resolved", "superseded"}


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
            or (ftype == "string_list" and isinstance(value, list)
                and all(isinstance(item, str) for item in value))
            or (ftype == "object" and isinstance(value, dict))
            or (ftype == "object_list" and isinstance(value, list)
                and all(isinstance(item, dict) for item in value))
        )
        if not ok:
            raise SchemaError(f"field {fname!r}: expected {ftype}, got {value!r}")


class DynamicStores:
    """Service over the same SQLite database as Store. Profile-scoped throughout."""

    def __init__(self, store: Store):
        self._store = store
        self.db.executescript(DYN_SCHEMA)
        columns = {r["name"] for r in self.db.execute(
            "PRAGMA table_info(dynamic_records)").fetchall()}
        if "updated_at" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE dynamic_records ADD COLUMN updated_at REAL")
        # The JSON expression index makes forum retries idempotent at the
        # database boundary, including concurrent retries from wake workers.
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_continuity_source "
            "ON dynamic_records(profile_id, store_name, "
            "json_extract(data, '$.source_type'), json_extract(data, '$.source_id')) "
            f"WHERE store_name='{THREAD_CONTINUITY_STORE}'"
        )

    @property
    def db(self):
        return self._store.db

    # -- lifecycle -------------------------------------------------------------

    def propose(self, profile_id: str, name: str, purpose: str,
                proposed_by: str, schema: dict) -> dict:
        self._store._require_profile(profile_id)
        if name == THREAD_CONTINUITY_STORE:
            self.ensure_thread_continuity(profile_id)
            raise DynStoreConflict("thread_continuity is a system-managed store")
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
        return self._approve_row(row, actor)

    def approve_id(self, store_id: str, actor: str = "admin") -> dict:
        row = self._require_id(store_id)
        return self._approve_row(row, actor)

    def _approve_row(self, row, actor: str) -> dict:
        if row["status"] != "pending":
            raise DynStoreConflict(f"only pending stores can be approved (is {row['status']})")
        with self.db:
            self.db.execute(
                "UPDATE dynamic_stores SET status='approved', approved_at=? WHERE id=?",
                (time.time(), row["id"]))
        self._audit(row["profile_id"], row["name"], "approved", actor,
                    f"v{row['version']}")
        return self._to_dict(self.db.execute(
            "SELECT * FROM dynamic_stores WHERE id=?", (row["id"],)).fetchone())

    def reject(self, profile_id: str, name: str, reason: str, actor: str = "admin") -> dict:
        row = self._require(profile_id, name)
        return self._reject_row(row, reason, actor)

    def reject_id(self, store_id: str, reason: str, actor: str = "admin") -> dict:
        row = self._require_id(store_id)
        return self._reject_row(row, reason, actor)

    def _reject_row(self, row, reason: str, actor: str) -> dict:
        if row["status"] != "pending":
            raise DynStoreConflict(f"only pending stores can be rejected (is {row['status']})")
        if not reason or not reason.strip():
            raise SchemaError("rejection reason is required")
        with self.db:
            self.db.execute(
                "UPDATE dynamic_stores SET status='rejected', rejected_at=?,"
                " rejection_reason=? WHERE id=?", (time.time(), reason, row["id"]))
        self._audit(row["profile_id"], row["name"], "rejected", actor,
                    f"v{row['version']}: {reason}")
        return self._to_dict(self.db.execute(
            "SELECT * FROM dynamic_stores WHERE id=?", (row["id"],)).fetchone())

    def archive(self, profile_id: str, name: str, actor: str = "admin") -> dict:
        if name == THREAD_CONTINUITY_STORE:
            raise DynStoreConflict("thread_continuity is a system-managed store")
        row = self._require(profile_id, name)
        if row["status"] != "approved":
            raise DynStoreConflict(f"only approved stores can be archived (is {row['status']})")
        with self.db:
            self.db.execute("UPDATE dynamic_stores SET status='archived' WHERE id=?",
                            (row["id"],))
        self._audit(profile_id, name, "archived", actor, f"v{row['version']}")
        return self.get(profile_id, name)

    def withdraw(self, profile_id: str, name: str, actor: str) -> dict:
        """A proposer may withdraw a pending store before an admin decides."""
        row = self._require(profile_id, name)
        return self._reject_row(row, "withdrawn by proposer", actor)

    def update_pending(self, profile_id: str, name: str, purpose: str,
                       schema: dict, actor: str) -> dict:
        if name == THREAD_CONTINUITY_STORE:
            raise DynStoreConflict("thread_continuity is a system-managed store")
        row = self._require(profile_id, name)
        if row["status"] != "pending":
            raise DynStoreConflict("only pending stores can be modified; archive and re-propose approved stores")
        if not purpose or not purpose.strip():
            raise SchemaError("purpose is required")
        validate_schema(schema)
        with self.db:
            self.db.execute("UPDATE dynamic_stores SET purpose=?, schema=? WHERE id=?",
                            (purpose, json.dumps(schema), row["id"]))
        self._audit(profile_id, name, "modified", actor, f"v{row['version']}: {purpose}")
        return self.get(profile_id, name)

    # -- records ---------------------------------------------------------------

    def add_record(self, profile_id: str, name: str, data: dict) -> dict:
        if name == THREAD_CONTINUITY_STORE:
            self.ensure_thread_continuity(profile_id)
            self._validate_thread_continuity(data)
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
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO dynamic_records (id, profile_id, store_name, schema_version,"
                    " data, created_at) VALUES (?,?,?,?,?,?)",
                    (rid, profile_id, name, row["version"], json.dumps(data), now))
        except sqlite3.IntegrityError:
            if name != THREAD_CONTINUITY_STORE:
                raise
            existing = self.db.execute(
                "SELECT id FROM dynamic_records WHERE profile_id=? AND store_name=? "
                "AND json_extract(data, '$.source_type')=? "
                "AND json_extract(data, '$.source_id')=?",
                (profile_id, name, data["source_type"], data["source_id"]),
            ).fetchone()
            if existing is None:
                raise
            return self.update_record(profile_id, name, existing["id"], data)
        return {"id": rid, "store": name, "schema_version": row["version"],
                "data": data, "created_at": now}

    def add_records(self, profile_id: str, name: str, records: list[dict]) -> list[dict]:
        if name == THREAD_CONTINUITY_STORE:
            raise DynStoreConflict(
                "thread_continuity uses add_record upserts; bulk import is disabled")
        if not records or len(records) > 200:
            raise SchemaError("records must contain between 1 and 200 items")
        row = self._latest_with_status(profile_id, name, ("approved",))
        if row is None:
            raise DynStoreConflict(f"store {name!r} has no approved version; bulk import rejected")
        schema = json.loads(row["schema"])
        for data in records:
            validate_record(schema, data)
        now = time.time()
        out = [{"id": str(uuid.uuid4()), "store": name, "schema_version": row["version"],
                "data": data, "created_at": now} for data in records]
        with self.db:
            self.db.executemany(
                "INSERT INTO dynamic_records (id, profile_id, store_name, schema_version, data, created_at)"
                " VALUES (?,?,?,?,?,?)", [(r["id"], profile_id, name, r["schema_version"],
                                             json.dumps(r["data"]), now) for r in out])
        return out

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
        return [self._record_dict(r) for r in self.db.execute(sql, params).fetchall()]

    def get_record(self, profile_id: str, name: str, record_id: str,
                   fields: list[str] | None = None) -> dict:
        self._require_queryable(profile_id, name)
        row = self._require_record(profile_id, name, record_id)
        return self._record_dict(row, fields)

    def update_record(self, profile_id: str, name: str, record_id: str,
                      patch: dict) -> dict:
        self._require_writable(profile_id, name)
        if not isinstance(patch, dict) or not patch:
            raise SchemaError("patch must be a non-empty object")
        row = self._require_record(profile_id, name, record_id)
        previous = json.loads(row["data"])
        if name == THREAD_CONTINUITY_STORE:
            for field in ("source_type", "source_id"):
                if field in patch and patch[field] != previous[field]:
                    raise SchemaError(
                        f"{field} is immutable; add_record with the new source instead")
        schema_row = self.db.execute(
            "SELECT schema FROM dynamic_stores WHERE profile_id=? AND name=? AND version=?",
            (profile_id, name, row["schema_version"])).fetchone()
        if schema_row is None:
            raise DynStoreConflict("record schema version no longer exists")
        data = {**previous, **patch}
        validate_record(json.loads(schema_row["schema"]), data)
        if name == THREAD_CONTINUITY_STORE:
            self._validate_thread_continuity(data)
        now = time.time()
        with self.db:
            self.db.execute("UPDATE dynamic_records SET data=?, updated_at=? WHERE id=?",
                            (json.dumps(data), now, record_id))
        self._audit(profile_id, name, "record_updated", profile_id, record_id)
        return self.get_record(profile_id, name, record_id)

    def delete_record(self, profile_id: str, name: str, record_id: str) -> dict:
        self._require_writable(profile_id, name)
        self._require_record(profile_id, name, record_id)
        with self.db:
            self.db.execute("DELETE FROM dynamic_records WHERE id=?", (record_id,))
        self._audit(profile_id, name, "record_deleted", profile_id, record_id)
        return {"deleted": True, "record_id": record_id, "store": name}

    def filter_records(self, profile_id: str, name: str,
                       where: dict | None = None, fields: list[str] | None = None,
                       order_by: str | None = None, descending: bool = True,
                       limit: int = 50) -> list[dict]:
        schema = self._require_queryable(profile_id, name)
        field_defs = schema["fields"]
        where = where or {}
        if not isinstance(where, dict):
            raise SchemaError("where must be an object")
        requested = set(where)
        if fields:
            requested.update(fields)
        if order_by:
            requested.add(order_by)
        unknown = requested - set(field_defs)
        if unknown:
            raise SchemaError(f"unknown query fields: {sorted(unknown)}")
        if not 1 <= limit <= 200:
            raise SchemaError("limit must be between 1 and 200")
        rows = self.db.execute(
            "SELECT * FROM dynamic_records WHERE profile_id=? AND store_name=?",
            (profile_id, name)).fetchall()
        records = [self._record_dict(r) for r in rows]
        records = [r for r in records if self._matches(r["data"], where)]
        if order_by:
            records.sort(key=lambda r: (r["data"].get(order_by) is None,
                                        r["data"].get(order_by)), reverse=descending)
        else:
            records.sort(key=lambda r: r["created_at"], reverse=descending)
        if fields:
            records = [{**r, "data": {k: r["data"][k] for k in fields
                                       if k in r["data"]}} for r in records]
        return records[:limit]

    def draw_weighted_records(self, profile_id: str, name: str, weight_field: str,
                              where: dict | None = None, count: int = 1) -> list[dict]:
        """Draw distinct matching records, with chance proportional to a numeric field.

        Zero-weight records are eligible for filtering but cannot be drawn.
        A draw is without replacement, so asking for several questions never
        returns the same record twice.
        """
        schema = self._require_queryable(profile_id, name)
        field_defs = schema["fields"]
        if weight_field not in field_defs:
            raise SchemaError(f"unknown weight field: {weight_field!r}")
        if field_defs[weight_field]["type"] not in {"number", "integer"}:
            raise SchemaError("weight_field must be a number or integer field")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 200:
            raise SchemaError("count must be an integer between 1 and 200")

        where = where or {}
        if not isinstance(where, dict):
            raise SchemaError("where must be an object")
        unknown = set(where) - set(field_defs)
        if unknown:
            raise SchemaError(f"unknown query fields: {sorted(unknown)}")
        rows = self.db.execute(
            "SELECT * FROM dynamic_records WHERE profile_id=? AND store_name=?",
            (profile_id, name)).fetchall()
        candidates = [self._record_dict(row) for row in rows]
        candidates = [record for record in candidates
                      if self._matches(record["data"], where)]

        weighted: list[tuple[dict, float]] = []
        for record in candidates:
            weight = record["data"].get(weight_field)
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise SchemaError(f"record {record['id']} has a non-numeric weight")
            numeric_weight = float(weight)
            if not math.isfinite(numeric_weight) or numeric_weight < 0:
                raise SchemaError(f"record {record['id']} has an invalid weight")
            if numeric_weight > 0:
                weighted.append((record, numeric_weight))
        if count > len(weighted):
            raise SchemaError("not enough positive-weight records for requested count")

        chooser = random.SystemRandom()
        selected: list[dict] = []
        while len(selected) < count:
            total = sum(weight for _, weight in weighted)
            threshold = chooser.random() * total
            running = 0.0
            for index, (record, weight) in enumerate(weighted):
                running += weight
                if threshold < running:
                    selected.append(record)
                    weighted.pop(index)
                    break
        return selected

    # -- introspection -----------------------------------------------------------

    def get(self, profile_id: str, name: str) -> dict:
        return self._to_dict(self._require(profile_id, name))

    def list(self, profile_id: str) -> list[dict]:
        self._store._require_profile(profile_id)
        self.ensure_thread_continuity(profile_id)
        rows = self.db.execute(
            "SELECT * FROM dynamic_stores WHERE profile_id=?"
            " ORDER BY name, version DESC", (profile_id,)).fetchall()
        latest, seen = [], set()
        for r in rows:  # latest version per name
            if r["name"] not in seen:
                seen.add(r["name"])
                latest.append(self._to_dict(r))
        return latest

    def ensure_thread_continuity(self, profile_id: str) -> dict:
        """Install the platform-owned continuity lane for one companion."""
        self._store._require_profile(profile_id)
        existing = self._latest(profile_id, THREAD_CONTINUITY_STORE)
        if existing is not None:
            return self._to_dict(existing)
        now = time.time()
        store_id = str(uuid.uuid4())
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO dynamic_stores (id, profile_id, name, version, purpose, "
                    "proposed_by, schema, status, created_at, approved_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (store_id, profile_id, THREAD_CONTINUITY_STORE, 1,
                     THREAD_CONTINUITY_PURPOSE, "system",
                     json.dumps(THREAD_CONTINUITY_SCHEMA), "approved", now, now),
                )
        except sqlite3.IntegrityError:
            pass
        return self.get(profile_id, THREAD_CONTINUITY_STORE)

    @staticmethod
    def _validate_thread_continuity(data: dict) -> None:
        if data.get("source_type") not in THREAD_SOURCE_TYPES:
            raise SchemaError(
                f"source_type must be one of {sorted(THREAD_SOURCE_TYPES)}")
        if data.get("status") not in THREAD_STATUSES:
            raise SchemaError(f"status must be one of {sorted(THREAD_STATUSES)}")
        limits = {
            "discussion_id": 200,
            "source_id": 200,
            "subject": 500,
            "position": 3000,
            "stake": 1500,
            "open_loop": 1500,
        }
        for field, limit in limits.items():
            value = data.get(field, "")
            if len(value) > limit:
                raise SchemaError(f"{field} must be at most {limit} characters")
        for field in ("occurred_at", "updated_at"):
            try:
                datetime.fromisoformat(data.get(field, "").replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                raise SchemaError(f"{field} must be an ISO-8601 timestamp") from None

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

    def _require_id(self, store_id: str):
        row = self.db.execute(
            "SELECT * FROM dynamic_stores WHERE id=?", (store_id,)).fetchone()
        if row is None:
            raise DynStoreNotFound("-", store_id)
        return row

    def _require_queryable(self, profile_id: str, name: str) -> dict:
        latest = self._require(profile_id, name)
        row = self._latest_with_status(profile_id, name, ("approved", "archived"))
        if row is None:
            raise DynStoreConflict(f"store {name!r} is {latest['status']}; not queryable")
        return json.loads(row["schema"])

    def _require_writable(self, profile_id: str, name: str):
        latest = self._require(profile_id, name)
        row = self._latest_with_status(profile_id, name, ("approved",))
        if row is None:
            raise DynStoreConflict(
                f"store {name!r} has no approved version (latest is"
                f" v{latest['version']}, {latest['status']}); writes rejected")
        return row

    def _require_record(self, profile_id: str, name: str, record_id: str):
        row = self.db.execute(
            "SELECT * FROM dynamic_records WHERE id=? AND profile_id=? AND store_name=?",
            (record_id, profile_id, name)).fetchone()
        if row is None:
            raise DynStoreNotFound(profile_id, f"{name}/{record_id}")
        return row

    @staticmethod
    def _record_dict(row, fields: list[str] | None = None) -> dict:
        result = dict(row)
        data = json.loads(result["data"])
        if fields is not None:
            data = {field: data[field] for field in fields if field in data}
        result["data"] = data
        result["store"] = result.pop("store_name")
        result.pop("profile_id", None)
        return result

    @staticmethod
    def _matches(data: dict, where: dict) -> bool:
        def compare(value, operator: str, operand) -> bool:
            try:
                if operator == "eq":
                    return value == operand
                if operator == "ne":
                    return value != operand
                if operator == "gt":
                    return value is not None and value > operand
                if operator == "gte":
                    return value is not None and value >= operand
                if operator == "lt":
                    return value is not None and value < operand
                if operator == "lte":
                    return value is not None and value <= operand
                if operator == "contains":
                    if isinstance(value, str) and isinstance(operand, str):
                        return operand.casefold() in value.casefold()
                    if isinstance(value, list):
                        return operand in value
                    return False
                if operator == "in":
                    return isinstance(operand, list) and value in operand
            except TypeError:
                return False
            raise SchemaError(
                f"unknown filter operator {operator!r}; use eq, ne, gt, gte, "
                "lt, lte, contains, or in")

        for field, condition in where.items():
            value = data.get(field)
            if isinstance(condition, dict):
                if not condition:
                    raise SchemaError(f"filter for {field!r} must not be empty")
                for operator, operand in condition.items():
                    if not compare(value, operator, operand):
                        return False
            elif value != condition:
                return False
        return True

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
