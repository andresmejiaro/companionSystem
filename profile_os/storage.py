"""SQLite + file storage for Assistant Profiles.

Layout (under a data directory, default ./data):
  data/
    profile_os.db                      SQLite: registry, events, state, domain records
    profiles/<id>/base_prompt.md       base behavior prompt (plain file, inspectable)
    profiles/<id>/role_prompt.md       role/lane prompt
    profiles/<id>/closeouts.jsonl      append-only closeout log (also mirrored in DB state)
    profiles/<id>/files/<name>         plain-file scratch store (scripts, notes) —
                                        never in git, never a DB blob; see write_file()
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .errors import (FileNotFoundInStore, MalformedMemoryEvent, MalformedMessage,
                     MalformedRecord, MemoryEventNotFound, MessageNotFound,
                     ProfileNotFound)

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    signature TEXT NOT NULL DEFAULT '',
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
    facts TEXT NOT NULL DEFAULT '',
    texture TEXT NOT NULL DEFAULT '',
    exchange TEXT NOT NULL DEFAULT '',
    new_state TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    from_profile_id TEXT NOT NULL REFERENCES profiles(id),
    to_profile_id TEXT NOT NULL REFERENCES profiles(id),
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    read_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_inbox ON messages(to_profile_id, created_at);
"""

MEMORY_KINDS = {"note", "fact", "decision", "failure_scar", "preference", "observation"}

DEFAULT_BOOT_EVENTS = 10
MAX_BOOT_EVENTS_CAP = 100


class Store:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.profiles_dir = self.data_dir / "profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "profile_os.db"
        # One connection per thread: FastAPI serves requests from a threadpool,
        # and SQLite connections are not safe to share across threads.
        self._local = threading.local()
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(closeouts)")}
        profile_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(profiles)")}
        with self.db:
            for name in ("facts", "texture", "exchange"):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE closeouts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            if "signature" not in profile_columns:
                self.db.execute("ALTER TABLE profiles ADD COLUMN signature TEXT NOT NULL DEFAULT ''")

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- profiles ------------------------------------------------------------

    def create_profile(
        self,
        profile_id: str,
        display_name: str,
        base_prompt: str,
        role_prompt: str,
        description: str = "",
        signature: str = "",
        allowed_tools: list[str] | None = None,
        memory_policy: dict | None = None,
        closeout_rules: str = "",
        initial_state: str = "",
    ) -> dict:
        if not profile_id or not profile_id.replace("-", "").replace("_", "").isalnum():
            raise MalformedRecord("profile_id must be a non-empty slug (alnum, - or _)")
        if not isinstance(description, str) or len(description) > 200:
            raise MalformedRecord("description must be a string of at most 200 characters")
        if not isinstance(signature, str) or len(signature) > 5:
            raise MalformedRecord("signature must be a string of at most 5 characters")
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO profiles (id, display_name, description, signature, allowed_tools,"
                " memory_policy, closeout_rules, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (profile_id, display_name, description, signature,
                 json.dumps(allowed_tools or []), json.dumps(memory_policy or {}),
                 closeout_rules, now),
            )
            self.db.execute(
                "INSERT INTO compact_state (profile_id, state, updated_at) VALUES (?,?,?)",
                (profile_id, initial_state, now),
            )
        pdir = self.profiles_dir / profile_id
        try:
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "base_prompt.md").write_text(base_prompt)
            (pdir / "role_prompt.md").write_text(role_prompt)
        except OSError:
            # Roll back the registration so we never keep a profile whose
            # prompt files are missing.
            with self.db:
                self.db.execute("DELETE FROM compact_state WHERE profile_id=?", (profile_id,))
                self.db.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
            shutil.rmtree(pdir, ignore_errors=True)
            raise
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
            "signature": row["signature"],
            "allowed_tools": json.loads(row["allowed_tools"]),
            "memory_policy": json.loads(row["memory_policy"]),
            "closeout_rules": row["closeout_rules"],
            "created_at": row["created_at"],
        }

    def list_profiles(self) -> list[dict]:
        rows = self.db.execute("SELECT id FROM profiles ORDER BY id").fetchall()
        return [self.get_profile(r["id"]) for r in rows]

    def delete_profile(self, profile_id: str) -> None:
        """Permanently remove a profile: registry row, state, memories,
        legacy domain records, and prompt files. Dynamic-store data lives in
        DynamicStores and is cleaned up separately by the caller (api.py)."""
        self._require_profile(profile_id)
        with self.db:
            self.db.execute("DELETE FROM domain_records WHERE profile_id=?", (profile_id,))
            self.db.execute("DELETE FROM memory_events WHERE profile_id=?", (profile_id,))
            self.db.execute("DELETE FROM closeouts WHERE profile_id=?", (profile_id,))
            self.db.execute("DELETE FROM compact_state WHERE profile_id=?", (profile_id,))
            self.db.execute(
                "DELETE FROM messages WHERE from_profile_id=? OR to_profile_id=?",
                (profile_id, profile_id))
            self.db.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        shutil.rmtree(self.profiles_dir / profile_id, ignore_errors=True)

    def _prompt(self, profile_id: str, name: str) -> str:
        path = self.profiles_dir / profile_id / name
        return path.read_text() if path.exists() else ""

    def update_prompts(self, profile_id: str, base_prompt: str | None = None,
                       role_prompt: str | None = None) -> dict:
        """Overwrite one or both prompt files. None leaves that file untouched."""
        self._require_profile(profile_id)
        pdir = self.profiles_dir / profile_id
        if base_prompt is not None:
            (pdir / "base_prompt.md").write_text(base_prompt)
        if role_prompt is not None:
            (pdir / "role_prompt.md").write_text(role_prompt)
        return self.get_profile(profile_id)

    def update_description(self, profile_id: str, description: str | None = None,
                           signature: str | None = None) -> dict:
        """Self-service, no approval: a companion maintains its own one-line
        'what I do', so other companions can discover who to ask via
        list_profiles instead of a human hardcoding names into prompts.
        Unlike base_prompt/role_prompt this isn't gated by TOTP — it's
        discovery metadata, not identity/behavior."""
        self._require_profile(profile_id)
        if description is None and signature is None:
            raise MalformedRecord("description or signature is required")
        if description is not None and (not isinstance(description, str) or len(description) > 200):
            raise MalformedRecord("description must be a string of at most 200 characters")
        if signature is not None and (not isinstance(signature, str) or len(signature) > 5):
            raise MalformedRecord("signature must be a string of at most 5 characters")
        changes, values = [], []
        if description is not None:
            changes.append("description=?")
            values.append(description)
        if signature is not None:
            changes.append("signature=?")
            values.append(signature)
        values.append(profile_id)
        with self.db:
            self.db.execute(f"UPDATE profiles SET {', '.join(changes)} WHERE id=?", values)
        return self.get_profile(profile_id)

    def recent_closeouts(self, profile_id: str, limit: int = 2) -> list[dict]:
        self._require_profile(profile_id)
        rows = self.db.execute(
            "SELECT * FROM closeouts WHERE profile_id=? ORDER BY created_at DESC LIMIT ?",
            (profile_id, limit)).fetchall()
        return [{"id": r["id"], "notes": r["notes"], "new_state": r["new_state"],
                 "created_at": r["created_at"]} for r in rows]

    def recent_exchanges(self, profile_id: str, limit: int = 4) -> list[dict]:
        """Return a small chronological set of semantic few-shot anchors.

        This is intentionally not a closeout archive: IDs, timestamps, facts,
        and notes remain out of session hydration. The model only receives the
        prior texture and verbatim exchanges that help it continue the voice.
        """
        self._require_profile(profile_id)
        rows = self.db.execute(
            "SELECT texture, exchange FROM closeouts WHERE profile_id=? "
            "ORDER BY created_at DESC LIMIT ?", (profile_id, limit)
        ).fetchall()
        return [
            {"texture": row["texture"], "exchange": row["exchange"]}
            for row in reversed(rows)
        ]

    def all_memories(self, profile_id: str) -> list[dict]:
        self._require_profile(profile_id)
        rows = self.db.execute(
            "SELECT * FROM memory_events WHERE profile_id=? ORDER BY created_at DESC",
            (profile_id,)).fetchall()
        return [self._event_dict(r) for r in rows]

    # -- core operations -----------------------------------------------------

    def boot(self, profile_id: str, recent_events: int | None = None) -> dict:
        """Everything needed to start a session for this profile.

        The number of recent memories honors the profile's
        memory_policy.max_boot_events when present and valid (an int in
        [0, MAX_BOOT_EVENTS_CAP]); otherwise DEFAULT_BOOT_EVENTS. An explicit
        recent_events argument overrides both.
        """
        profile = self.get_profile(profile_id)
        if recent_events is None:
            policy = profile["memory_policy"]
            mbe = policy.get("max_boot_events") if isinstance(policy, dict) else None
            if (isinstance(mbe, int) and not isinstance(mbe, bool)
                    and 0 <= mbe <= MAX_BOOT_EVENTS_CAP):
                recent_events = mbe
            else:
                recent_events = DEFAULT_BOOT_EVENTS
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

    def _require_memory(self, profile_id: str, event_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM memory_events WHERE id=? AND profile_id=?",
            (event_id, profile_id)).fetchone()
        if row is None:
            raise MemoryEventNotFound(profile_id, event_id)
        return row

    def update_memory(self, profile_id: str, event_id: str, kind: str | None = None,
                      content: str | None = None, tags: list[str] | None = None) -> dict:
        """Self-service edit: a companion may revise its own memory events.
        Not a backend/admin action — same trust level as remember()."""
        self._require_profile(profile_id)
        self._require_memory(profile_id, event_id)
        if kind is not None and kind not in MEMORY_KINDS:
            raise MalformedMemoryEvent(f"kind must be one of {sorted(MEMORY_KINDS)}")
        if content is not None and (not isinstance(content, str) or not content.strip()):
            raise MalformedMemoryEvent("content must be a non-empty string")
        if tags is not None and (not isinstance(tags, list)
                                 or not all(isinstance(t, str) for t in tags)):
            raise MalformedMemoryEvent("tags must be a list of strings")
        sets, params = [], []
        if kind is not None:
            sets.append("kind=?")
            params.append(kind)
        if content is not None:
            sets.append("content=?")
            params.append(content)
        if tags is not None:
            sets.append("tags=?")
            params.append(json.dumps(tags))
        if sets:
            params += [event_id, profile_id]
            with self.db:
                self.db.execute(
                    f"UPDATE memory_events SET {', '.join(sets)} WHERE id=? AND profile_id=?",
                    params)
        return self._event_dict(self.db.execute(
            "SELECT * FROM memory_events WHERE id=?", (event_id,)).fetchone())

    def delete_memory(self, profile_id: str, event_id: str) -> None:
        """Self-service erase: a companion may remove its own memory events."""
        self._require_profile(profile_id)
        self._require_memory(profile_id, event_id)
        with self.db:
            self.db.execute(
                "DELETE FROM memory_events WHERE id=? AND profile_id=?",
                (event_id, profile_id))

    # -- inbox (companion-to-companion messages) --------------------------------

    def send_message(self, from_profile_id: str, to_profile_id: str, content: str) -> dict:
        """No approval, no backend action beyond the write itself — same
        trust level as remember(). Lets a companion leave a message in
        another profile's inbox instead of a human copy-pasting between
        conversations."""
        self._require_profile(from_profile_id)
        self._require_profile(to_profile_id)
        if not content or not isinstance(content, str) or not content.strip():
            raise MalformedMessage("content must be a non-empty string")
        mid = str(uuid.uuid4())
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO messages (id, from_profile_id, to_profile_id, content,"
                " created_at) VALUES (?,?,?,?,?)",
                (mid, from_profile_id, to_profile_id, content, now))
        return self._message_dict(self.db.execute(
            "SELECT * FROM messages WHERE id=?", (mid,)).fetchone())

    def list_inbox(self, profile_id: str, unread_only: bool = False,
                   limit: int = 50) -> list[dict]:
        self._require_profile(profile_id)
        sql = "SELECT * FROM messages WHERE to_profile_id=?"
        params: list = [profile_id]
        if unread_only:
            sql += " AND read_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.db.execute(sql, params).fetchall()
        return [self._message_dict(r) for r in rows]

    def mark_message_read(self, profile_id: str, message_id: str) -> dict:
        self._require_profile(profile_id)
        row = self.db.execute(
            "SELECT * FROM messages WHERE id=? AND to_profile_id=?",
            (message_id, profile_id)).fetchone()
        if row is None:
            raise MessageNotFound(profile_id, message_id)
        with self.db:
            self.db.execute("UPDATE messages SET read_at=? WHERE id=?",
                            (time.time(), message_id))
        return self._message_dict(self.db.execute(
            "SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "from_profile_id": row["from_profile_id"],
                "to_profile_id": row["to_profile_id"], "content": row["content"],
                "created_at": row["created_at"], "read_at": row["read_at"]}

    # -- file store (plain files on disk — scripts, notes; never git, never a DB blob) --

    _FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
    MAX_FILE_BYTES = 256 * 1024

    @classmethod
    def _validate_filename(cls, filename: str) -> None:
        if (not cls._FILENAME_RE.match(filename or "")
                or filename in (".", "..") or ".." in filename):
            raise MalformedRecord(
                "filename must match [A-Za-z0-9._-]{1,128}, no path separators")

    def _files_dir(self, profile_id: str) -> Path:
        d = self.profiles_dir / profile_id / "files"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_file(self, profile_id: str, filename: str, content: str) -> dict:
        """Self-service: same trust level as remember(). Overwrites if it
        already exists — this is a scratch store, not versioned storage."""
        self._require_profile(profile_id)
        self._validate_filename(filename)
        if not isinstance(content, str):
            raise MalformedRecord("content must be a string")
        data = content.encode("utf-8")
        if len(data) > self.MAX_FILE_BYTES:
            raise MalformedRecord(f"file too large (max {self.MAX_FILE_BYTES} bytes)")
        (self._files_dir(profile_id) / filename).write_bytes(data)
        return self._file_meta(profile_id, filename)

    def read_file(self, profile_id: str, filename: str) -> dict:
        self._require_profile(profile_id)
        self._validate_filename(filename)
        path = self._files_dir(profile_id) / filename
        if not path.is_file():
            raise FileNotFoundInStore(profile_id, filename)
        return {**self._file_meta(profile_id, filename), "content": path.read_text()}

    def list_files(self, profile_id: str) -> list[dict]:
        self._require_profile(profile_id)
        d = self._files_dir(profile_id)
        return [self._file_meta(profile_id, p.name)
                for p in sorted(d.iterdir()) if p.is_file()]

    def delete_file(self, profile_id: str, filename: str) -> None:
        self._require_profile(profile_id)
        self._validate_filename(filename)
        path = self._files_dir(profile_id) / filename
        if not path.is_file():
            raise FileNotFoundInStore(profile_id, filename)
        path.unlink()

    def _file_meta(self, profile_id: str, filename: str) -> dict:
        stat = (self._files_dir(profile_id) / filename).stat()
        return {"filename": filename, "size": stat.st_size, "updated_at": stat.st_mtime}

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

    def closeout(self, profile_id: str, facts: str, texture: str, exchange: str,
                 notes: str = "") -> dict:
        """End a session with a compact, voice-preserving handoff."""
        self._require_profile(profile_id)
        fields = {"facts": facts, "texture": texture, "exchange": exchange}
        limits = {"facts": 1200, "texture": 700, "exchange": 800, "notes": 700}
        for name, value in {**fields, "notes": notes}.items():
            if not isinstance(value, str) or (name != "notes" and not value.strip()):
                raise MalformedRecord(f"closeout requires non-empty {name}")
            if len(value) > limits[name]:
                raise MalformedRecord(f"closeout {name} exceeds {limits[name]} characters")
        new_state = "\n\n".join(
            [f"## Facts\n{facts.strip()}", f"## Texture\n{texture.strip()}",
             f"## Meaningful exchange\n{exchange.strip()}"] +
            ([f"## Notes\n{notes.strip()}"] if notes.strip() else []))
        cid = str(uuid.uuid4())
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO closeouts (id, profile_id, notes, facts, texture, exchange, new_state, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)", (cid, profile_id, notes, facts, texture, exchange, new_state, now))
            self.db.execute(
                "UPDATE compact_state SET state=?, updated_at=? WHERE profile_id=?",
                (new_state, now, profile_id))
        line = json.dumps({"id": cid, "facts": facts, "texture": texture,
                           "exchange": exchange, "notes": notes, "new_state": new_state, "at": now})
        with open(self.profiles_dir / profile_id / "closeouts.jsonl", "a") as f:
            f.write(line + "\n")
        return {"id": cid, "profile_id": profile_id, "facts": facts, "texture": texture,
                "exchange": exchange, "notes": notes, "new_state": new_state, "created_at": now}

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
