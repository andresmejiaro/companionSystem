"""Shared, schema-enforced project stores with explicit companion membership."""

from __future__ import annotations

import json
import re
import time
import uuid

from .dynstores import validate_record, validate_schema
from .errors import DynStoreConflict, DynStoreNotFound, ProfileNotFound, SchemaError
from .storage import Store

PROJECT_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL,
    schema TEXT NOT NULL,
    created_by_profile_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    approved_at REAL
);
CREATE TABLE IF NOT EXISTS project_members (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'editor',
    joined_at REAL NOT NULL,
    PRIMARY KEY(project_id, profile_id)
);
CREATE TABLE IF NOT EXISTS project_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    created_by_profile_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_records ON project_records(project_id, created_at);
CREATE TABLE IF NOT EXISTS admin_notifications (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    created_at REAL NOT NULL,
    silenced_at REAL,
    resolved_at REAL
);
"""


class Projects:
    def __init__(self, store: Store):
        self.store = store
        self.db.executescript(PROJECT_SCHEMA)

    @property
    def db(self):
        return self.store.db

    def propose_create(self, profile_id: str, name: str, purpose: str, schema: dict) -> dict:
        self.store._require_profile(profile_id)
        if not PROJECT_RE.match(name or ""):
            raise SchemaError("project name must be a lowercase slug")
        if not purpose or not purpose.strip():
            raise SchemaError("purpose is required")
        validate_schema(schema)
        if self.db.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
            raise DynStoreConflict(f"project {name!r} already exists")
        project_id, now = str(uuid.uuid4()), time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO projects(id,name,purpose,schema,created_by_profile_id,status,created_at)"
                " VALUES(?,?,?,?,?,'pending',?)",
                (project_id, name, purpose.strip(), json.dumps(schema), profile_id, now))
        return self.get(project_id, include_pending=True)

    def approve_create(self, project_id: str) -> dict:
        project = self.get(project_id, include_pending=True)
        if project["status"] != "pending":
            raise DynStoreConflict("project is not pending")
        now = time.time()
        with self.db:
            self.db.execute("UPDATE projects SET status='active', approved_at=? WHERE id=?",
                            (now, project_id))
            self.db.execute(
                "INSERT INTO project_members(project_id,profile_id,role,joined_at) VALUES(?,?,?,?)",
                (project_id, project["created_by_profile_id"], "owner", now))
        return self.get(project_id)

    def reject_create(self, project_id: str) -> None:
        project = self.get(project_id, include_pending=True)
        if project["status"] == "pending":
            with self.db:
                self.db.execute("DELETE FROM projects WHERE id=?", (project_id,))

    def request_join(self, profile_id: str, project_id: str) -> dict:
        self.store._require_profile(profile_id)
        project = self.get(project_id)
        if self.is_member(project_id, profile_id):
            raise DynStoreConflict(f"{profile_id!r} is already a member")
        return project

    def approve_join(self, profile_id: str, project_id: str) -> dict:
        self.request_join(profile_id, project_id)
        with self.db:
            self.db.execute(
                "INSERT INTO project_members(project_id,profile_id,role,joined_at) VALUES(?,?,?,?)",
                (project_id, profile_id, "editor", time.time()))
        return self.get(project_id, viewer_profile_id=profile_id)

    def leave(self, profile_id: str, project_id: str) -> dict:
        project = self.get(project_id)
        if not self.is_member(project_id, profile_id):
            raise DynStoreConflict(f"{profile_id!r} is not a project member")
        with self.db:
            self.db.execute("DELETE FROM project_members WHERE project_id=? AND profile_id=?",
                            (project_id, profile_id))
        if not self._members(project_id):
            self._notify_empty(project, profile_id)
        return {"left": True, "project_id": project_id, "empty": not self._members(project_id)}

    def list_for(self, profile_id: str) -> list[dict]:
        self.store._require_profile(profile_id)
        ids = [r["project_id"] for r in self.db.execute(
            "SELECT project_id FROM project_members WHERE profile_id=?", (profile_id,)).fetchall()]
        return [self.get(i, viewer_profile_id=profile_id) for i in ids]

    def list_available(self, profile_id: str) -> list[dict]:
        self.store._require_profile(profile_id)
        rows = self.db.execute("SELECT id FROM projects WHERE status='active' ORDER BY name").fetchall()
        return [self.get(r["id"], viewer_profile_id=profile_id) for r in rows]

    def add_record(self, profile_id: str, project_id: str, data: dict) -> dict:
        project = self._require_member(profile_id, project_id)
        validate_record(project["schema"], data)
        rid, now = str(uuid.uuid4()), time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO project_records(id,project_id,data,created_by_profile_id,created_at)"
                " VALUES(?,?,?,?,?)", (rid, project_id, json.dumps(data), profile_id, now))
        return {"id": rid, "project_id": project_id, "data": data,
                "created_by_profile_id": profile_id, "created_at": now}

    def query(self, profile_id: str, project_id: str, contains: str | None = None,
              limit: int = 50) -> list[dict]:
        self._require_member(profile_id, project_id)
        sql = "SELECT * FROM project_records WHERE project_id=?"
        params: list = [project_id]
        if contains:
            sql += " AND data LIKE ? COLLATE NOCASE"
            params.append(f"%{contains}%")
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [{**dict(r), "data": json.loads(r["data"])}
                for r in self.db.execute(sql, params).fetchall()]

    def notifications(self, include_silenced: bool = False) -> list[dict]:
        sql = "SELECT * FROM admin_notifications WHERE resolved_at IS NULL"
        if not include_silenced:
            sql += " AND silenced_at IS NULL"
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self.db.execute(sql).fetchall()]

    def silence(self, notification_id: str) -> dict:
        with self.db:
            cur = self.db.execute(
                "UPDATE admin_notifications SET silenced_at=? WHERE id=? AND resolved_at IS NULL",
                (time.time(), notification_id))
        if not cur.rowcount:
            raise DynStoreNotFound("notification", notification_id)
        return dict(self.db.execute("SELECT * FROM admin_notifications WHERE id=?",
                                    (notification_id,)).fetchone())

    def delete(self, project_id: str) -> dict:
        project = self.get(project_id)
        if self._members(project_id):
            raise DynStoreConflict("only an empty project can be deleted")
        with self.db:
            self.db.execute("DELETE FROM project_records WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM admin_notifications WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        return {"deleted": True, "project_id": project_id}

    def get(self, project_id: str, viewer_profile_id: str | None = None,
            include_pending: bool = False) -> dict:
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None or (row["status"] != "active" and not include_pending):
            raise DynStoreNotFound("project", project_id)
        result = dict(row)
        result["schema"] = json.loads(result["schema"])
        result["members"] = self._members(project_id)
        if viewer_profile_id is not None:
            result["viewer_is_member"] = self.is_member(project_id, viewer_profile_id)
        return result

    def is_member(self, project_id: str, profile_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM project_members WHERE project_id=? AND profile_id=?",
            (project_id, profile_id)).fetchone() is not None

    def _require_member(self, profile_id: str, project_id: str) -> dict:
        self.store._require_profile(profile_id)
        project = self.get(project_id)
        if not self.is_member(project_id, profile_id):
            raise DynStoreConflict(f"{profile_id!r} is not a project member")
        return project

    def _members(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT profile_id,role,joined_at FROM project_members WHERE project_id=?"
            " ORDER BY joined_at", (project_id,)).fetchall()]

    def _notify_empty(self, project: dict, departed_profile_id: str) -> None:
        message = (f"Project {project['name']!r} has no companions left. "
                   "Should it be kept, silenced, or deleted in Settings?")
        nid = str(uuid.uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO admin_notifications(id,kind,project_id,message,created_at)"
                " VALUES(?,?,?,?,?)", (nid, "empty_project", project["id"], message, time.time()))
        for profile in self.store.list_profiles():
            self.store.send_message(departed_profile_id, profile["id"], message)
