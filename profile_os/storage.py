"""SQLite + file storage for Assistant Profiles.

Layout (under a data directory, default ./data):
  data/
    profile_os.db                      SQLite: registry, events, state, domain records
    profiles/<id>/base_prompt.md       base behavior prompt (plain file, inspectable)
    profiles/<id>/role_prompt.md       role/lane prompt
    profiles/<id>/closeouts.jsonl      append-only closeout log (also mirrored in DB state)
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .errors import MalformedMemoryEvent, MalformedRecord, ProfileNotFound

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    allowed_tools TEXT NOT NULL DEFAULT '[]',      -- JSON list
    memory_policy TEXT NOT NULL DEFAULT '{}',      -- JSON object
    closeout_rules TEXT NOT NULL DEFAULT '',       -- free text rules
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS compact_state (
    profile_id TEXT PRIMARY KEY REFERENCES profiles(id),
    state TEXT NOT NULL DEFAULT '',                -- compact markdown/plaintext boot state
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_events (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id),
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',               -- JSON list
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_profile ON memory_events(profile_id, created_at);
CREATE TABLE IF NOT EXISTS domain_records (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id),
    store TEXT NOT NULL,                           -- e.g. 'foods', 'products', 'task_contracts'
    data TEXT NOT NULL,                            -- JSON object
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_domain ON domain_records(profile_id, store);
CREATE TABLE IF NOT EXISTS closeouts (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id),
    notes TEXT NOT NULL,
    new_state TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

MEMORY_KINDS = {"note", "fact", "decision", "failure_scar", "preference", "observation"}


class Store:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.profiles_dir = self.data_dir / "profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI's threadpool serves requests from
        # worker threads; access is short-lived per request in slice zero.
        self.db = sqlite3.connect(self.data_dir / "profile_os.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # -- profiles ------------------------------------------------------------

    def create_profile(
        self,
        profile_id: str,
        display_name: str,
        base_prompt: str,
        role_prompt: str,
        description: str = "",
        allowed_tools: list[str] | None = None,
        memory_policy: dict | None = None,
        closeout_rules: str = "",
        initial_state: str = "",
    ) -> dict:
        if not profile_id or not profile_id.replace("-", "").replace("_", "").isalnum():
            raise MalformedRecord("profile_id must be a non-empty slug (alnum, - or _)")
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO profiles (id, display_name, description, allowed_tools,"
                " memory_policy, closeout_rules, created_at) VALUES (?,?,?,?,?,?,?)",
                (profile_id, display_name, description,
                 json.dumps(allowed_tools or []), json.dumps(memory_policy or {}),
                 closeout_rules, now),
            )
            self.db.execute(
                "INSERT INTO compact_state (profile_id, state, updated_at) VALUES (?,?,?)",
                (profile_id, initial_state, now),
            )
        pdir = self.profiles_dir / profile_id
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "base_prompt.md").write_text(base_prompt)
        (pdir / "role_prompt.md").write_text(role_prompt)
        return self.get_profile(profile_id)

    def _require_profile(self, profile_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if row is None:
            raise ProfileNotFound(profile_id)
        return row

    def get_profile(self, profile_id: str) -> dict:
        row = self._require_profile(profile_id)
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "description": row["description"],
            "allowed_tools": json.loads(row["allowed_tools"]),
            "memory_policy": json.loads(row["memory_policy"]),
            "closeout_rules": row["closeout_rules"],
            "created_at": row["created_at"],
        }

    def list_profiles(self) -> list[dict]:
        rows = self.db.execute("SELECT id FROM profiles ORDER BY id").fetchall()
        return [self.get_profile(r["id"]) for r in rows]

    def _prompt(self, profile_id: str, name: str) -> str:
        path = self.profiles_dir / profile_id / name
        return path.read_text() if path.exists() else ""

    # -- core operations -----------------------------------------------------

    def boot(self, profile_id: str, recent_events: int = 10) -> dict:
        """Everything needed to start a session for this profile."""
        profile = self.get_profile(profile_id)
        state_row = self.db.execute(
            "SELECT state, updated_at FROM compact_state WHERE profile_id=?", (profile_id,)
        ).fetchone()
        events = self.db.execute(
            "SELECT * FROM memory_events WHERE profile_id=? ORDER BY created_at DESC LIMIT ?",
            (profile_id, recent_events),
        ).fetchall()
        return {
            "profile": profile,
            "base_prompt": self._prompt(profile_id, "base_prompt.md"),
            "role_prompt": self._prompt(profile_id, "role_prompt.md"),
            "compact_state": state_row["state"] if state_row else "",
            "state_updated_at": state_row["updated_at"] if state_row else None,
            "recent_memories": [self._event_dict(e) for e in events],
        }

    def remember(self, profile_id: str, event: dict) -> dict:
        self._require_profile(profile_id)
        if not isinstance(event, dict):
            raise MalformedMemoryEvent("event must be an object")
        kind = event.get("kind")
        content = event.get("content")
        if not kind or kind not in MEMORY_KINDS:
            raise MalformedMemoryEvent(f"kind must be one of {sorted(MEMORY_KINDS)}")
        if not content or not isinstance(content, str) or not content.strip():
            raise MalformedMemoryEvent("content must be a non-empty string")
        tags = event.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise MalformedMemoryEvent("tags must be a list of strings")
        eid = str(uuid.uuid4())
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO memory_events (id, profile_id, kind, content, tags, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (eid, profile_id, kind, content, json.dumps(tags), now),
            )
        return self._event_dict(self.db.execute(
            "SELECT * FROM memory_events WHERE id=?", (eid,)).fetchone())

    def search(self, profile_id: str, query: str, limit: int = 20) -> list[dict]:
        """Case-insensitive substring search over content and tags. Boring on purpose."""
        self._require_profile(profile_id)
        like = f"%{query}%"
        rows = self.db.execute(
            "SELECT * FROM memory_events WHERE profile_id=? AND"
            " (content LIKE ? COLLATE NOCASE OR tags LIKE ? COLLATE NOCASE)"
            " ORDER BY created_at DESC LIMIT ?",
            (profile_id, like, like, limit),
        ).fetchall()
        return [self._event_dict(r) for r in rows]

    def closeout(self, profile_id: str, notes: str, new_state: str) -> dict:
        """End a session: log the closeout and replace compact state."""
        self._require_profile(profile_id)
        if not new_state or not new_state.strip():
            raise MalformedRecord("closeout requires a non-empty new_state")
        cid = str(uuid.uuid4())
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO closeouts (id, profile_id, notes, new_state, created_at)"
                " VALUES (?,?,?,?,?)", (cid, profile_id, notes, new_state, now))
            self.db.execute(
                "UPDATE compact_state SET state=?, updated_at=? WHERE profile_id=?",
                (new_state, now, profile_id))
        line = json.dumps({"id": cid, "notes": notes, "new_state": new_state, "at": now})
        with open(self.profiles_dir / profile_id / "closeouts.jsonl", "a") as f:
            f.write(line + "\n")
        return {"id": cid, "profile_id": profile_id, "notes": notes,
                "new_state": new_state, "created_at": now}

    # -- domain stores ---------------------------------------------------------

    def add_domain_record(self, profile_id: str, store: str, data: dict) -> dict:
        self._require_profile(profile_id)
        if not store or not isinstance(data, dict) or not data:
            raise MalformedRecord("store name and non-empty data object required")
        rid = str(uuid.uuid4())
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO domain_records (id, profile_id, store, data, created_at)"
                " VALUES (?,?,?,?,?)", (rid, profile_id, store, json.dumps(data), now))
        return {"id": rid, "store": store, "data": data, "created_at": now}

    def query_domain(self, profile_id: str, store: str,
                     contains: str | None = None, limit: int = 50) -> list[dict]:
        self._require_profile(profile_id)
        sql = "SELECT * FROM domain_records WHERE profile_id=? AND store=?"
        params: list = [profile_id, store]
        if contains:
            sql += " AND data LIKE ? COLLATE NOCASE"
            params.append(f"%{contains}%")
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.db.execute(sql, params).fetchall()
        return [{"id": r["id"], "store": r["store"], "data": json.loads(r["data"]),
                 "created_at": r["created_at"]} for r in rows]

    def list_domain_stores(self, profile_id: str) -> list[str]:
        self._require_profile(profile_id)
        rows = self.db.execute(
            "SELECT DISTINCT store FROM domain_records WHERE profile_id=? ORDER BY store",
            (profile_id,)).fetchall()
        return [r["store"] for r in rows]

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "profile_id": row["profile_id"], "kind": row["kind"],
                "content": row["content"], "tags": json.loads(row["tags"]),
                "created_at": row["created_at"]}
