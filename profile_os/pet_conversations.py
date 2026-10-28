"""Durable text conversation lifecycle for Tamagochi.

Runtime adapters implement ``run(profile_id, history, text, identity,
checkpoint)`` and return reply text. They must call checkpoint before costly
work and before dispatching any side effect. This service never replays work
after restart; interrupted nonterminal turns become ``unknown``.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from .pet_devices import PetDeviceService

SCHEMA = """
CREATE TABLE IF NOT EXISTS pet_conversations (
 id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, credential_id TEXT NOT NULL,
 profile_id TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pet_turns (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES pet_conversations(id),
 principal_id TEXT NOT NULL, credential_id TEXT NOT NULL, profile_id TEXT NOT NULL,
 request_id TEXT NOT NULL, payload_hash TEXT NOT NULL, text TEXT NOT NULL,
 surface TEXT NOT NULL, stage TEXT NOT NULL, revision INTEGER NOT NULL,
 transcript TEXT, reply_text TEXT, speech_status TEXT NOT NULL, error TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(principal_id, request_id));
CREATE INDEX IF NOT EXISTS pet_turns_conversation_stage ON pet_turns(conversation_id,stage);
"""
NONTERMINAL = ("accepted", "responding", "cancel_requested")
TERMINAL = ("completed", "failed", "cancelled", "unknown")


class RuntimeOutcomeUnknown(Exception):
    """The provider may have started; safe automatic replay is impossible."""


class RuntimeUnavailable(Exception):
    """No conversation runtime is configured."""


class RuntimeStopped(Exception):
    """A checkpoint stopped provider work due to cancellation or revocation."""


class PetTurnService:
    def __init__(self, store, devices: PetDeviceService, runtime=None,
                 max_workers: int = 4, max_pending: int = 16):
        self.store, self.devices, self.runtime = store, devices, runtime
        self.max_pending = max_pending
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="pet-turn")
        self._lock = threading.Lock()
        self._queued: set[str] = set()
        self._flock = None
        self.running = False
        self.store.db.executescript(SCHEMA)

    def start(self):
        self.running = True
        # Ordinary Companions deployments with no pet runtime do not acquire
        # lifecycle ownership or perform pet recovery.
        if self.runtime is None:
            return
        # Keep the exclusive lock for the app lifetime: only one process may
        # interpret nonterminal rows as abandoned during recovery.
        path = self.store.data_dir / "pet_turns.lock"
        self._flock = open(path, "a+b")
        try:
            fcntl.flock(self._flock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._flock.close(); self._flock = None
            raise RuntimeError("another pet turn executor owns this store")
        with self.store.db:
            self.store.db.execute("UPDATE pet_turns SET stage='unknown',revision=revision+1,"
                                  "error='service_restarted',updated_at=strftime('%s','now') "
                                  "WHERE stage IN ('accepted','responding','cancel_requested')")

    def stop(self):
        with self._lock:
            self.running = False
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            try:
                if self.runtime is not None:
                    close = getattr(self.runtime, "close", None)
                    if callable(close):
                        close()
            finally:
                try:
                    self.store.close()
                finally:
                    if self._flock is not None:
                        fcntl.flock(self._flock.fileno(), fcntl.LOCK_UN)
                        self._flock.close()
                        self._flock = None

    def authorized(self, identity, profile_id):
        return self.devices.authorize(identity["principal_id"], profile_id,
                                      "pet:converse", identity["credential_id"])

    def runtime_supports(self, profile_id):
        if not self.running or self.runtime is None:
            return False
        supports = getattr(self.runtime, "supports", None)
        return bool(supports(profile_id)) if supports is not None else True

    def _row(self, turn_id):
        return self.store.db.execute("SELECT * FROM pet_turns WHERE id=?", (turn_id,)).fetchone()

    @staticmethod
    def public(row):
        return {"turn_id": row["id"], "conversation_id": row["conversation_id"],
                "profile_id": row["profile_id"], "request_id": row["request_id"],
                "stage": row["stage"], "revision": row["revision"],
                "transcript": row["transcript"], "reply_text": row["reply_text"],
                "speech_status": row["speech_status"], "error": row["error"]}

    def create_conversation(self, identity, profile_id, conversation_id=None):
        import time
        if not self.authorized(identity, profile_id):
            raise PermissionError
        db = self.store.db
        if conversation_id is not None:
            row = db.execute("SELECT * FROM pet_conversations WHERE id=? AND principal_id=? "
                             "AND credential_id=? AND profile_id=?",
                             (conversation_id, identity["principal_id"], identity["credential_id"], profile_id)).fetchone()
            if row is None: raise LookupError
            return {"conversation_id": row["id"], "profile_id": profile_id}
        cid = str(uuid.uuid4())
        with db:
            db.execute("INSERT INTO pet_conversations VALUES (?,?,?,?,?)",
                       (cid, identity["principal_id"], identity["credential_id"], profile_id, time.time()))
        return {"conversation_id": cid, "profile_id": profile_id}

    def submit(self, identity, conversation_id, request_id, text, surface):
        import time
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError("invalid_request_id")
        if surface != "pet_text": raise ValueError("unsupported_surface")
        if not isinstance(text, str) or not text.strip(): raise ValueError("invalid_text")
        if len(text.encode("utf-8")) > 16384: raise OverflowError
        db = self.store.db
        convo = db.execute("SELECT * FROM pet_conversations WHERE id=? AND principal_id=? AND credential_id=?",
                           (conversation_id, identity["principal_id"], identity["credential_id"])).fetchone()
        if convo is None: raise LookupError
        if not self.authorized(identity, convo["profile_id"]): raise PermissionError
        digest = hashlib.sha256(json.dumps([conversation_id, text, surface], ensure_ascii=False,
                                           separators=(",", ":")).encode()).hexdigest()
        existing = db.execute("SELECT * FROM pet_turns WHERE principal_id=? AND request_id=?",
                              (identity["principal_id"], request_id)).fetchone()
        if existing:
            if existing["payload_hash"] != digest: raise FileExistsError
            return self.public(existing), False
        if not self.running:
            raise RuntimeUnavailable
        if not self.runtime_supports(convo["profile_id"]):
            raise RuntimeUnavailable
        now = time.time(); tid = str(uuid.uuid4())
        try:
            # BEGIN IMMEDIATE serializes active-turn and queue-cap checks.
            db.execute("BEGIN IMMEDIATE")
            # Repeat dedup inside the write lock so simultaneous identical
            # submissions resolve to the first durable row.
            existing = db.execute("SELECT * FROM pet_turns WHERE principal_id=? AND request_id=?",
                                  (identity["principal_id"], request_id)).fetchone()
            if existing:
                db.rollback()
                if existing["payload_hash"] != digest: raise FileExistsError
                return self.public(existing), False
            active = db.execute("SELECT 1 FROM pet_turns WHERE conversation_id=? AND stage IN "
                                "('accepted','responding','cancel_requested','unknown') LIMIT 1", (conversation_id,)).fetchone()
            count = db.execute("SELECT COUNT(*) n FROM pet_turns WHERE stage IN "
                               "('accepted','responding','cancel_requested')").fetchone()["n"]
            if active or count >= self.max_pending: db.rollback(); raise BlockingIOError
            db.execute("INSERT INTO pet_turns VALUES (?,?,?,?,?,?,?,?,?,'accepted',1,?,NULL,'not_requested',NULL,?,?)",
                       (tid, conversation_id, identity["principal_id"], identity["credential_id"],
                        convo["profile_id"], request_id, digest, text, surface, text, now, now))
            db.commit()
        except Exception:
            if db.in_transaction: db.rollback()
            raise
        with self._lock:
            if not self.running:
                self._set(tid, "unknown", error="shutdown_before_dispatch")
                raise RuntimeUnavailable
            self._queued.add(tid)
            try:
                self.executor.submit(self._run, tid,
                                     {**identity, "profile_id": convo["profile_id"]})
            except RuntimeError:
                self._queued.discard(tid)
                self._set(tid, "unknown", error="dispatch_unavailable")
                raise RuntimeUnavailable
        return self.public(self._row(tid)), True

    def _run(self, turn_id, identity):
        db = self.store.db
        try:
            row = self._row(turn_id)
            if row is None or row["stage"] != "accepted": return
            if not self.authorized(identity, row["profile_id"]):
                self._set(turn_id, "failed", error="authorization_revoked"); return
            if self.runtime is None:
                self._set(turn_id, "failed", error="runtime_unavailable"); return
            with db:
                changed = db.execute("UPDATE pet_turns SET stage='responding',revision=revision+1,updated_at=? "
                                     "WHERE id=? AND stage='accepted'", (__import__('time').time(), turn_id)).rowcount
            if not changed: return
            history_rows = db.execute("SELECT text,reply_text FROM pet_turns WHERE conversation_id=? "
                                      "AND stage='completed' ORDER BY created_at DESC LIMIT 10",
                                      (row["conversation_id"],)).fetchall()
            history = [{"text": x["text"][:16384], "reply_text": (x["reply_text"] or "")[:16384]}
                       for x in reversed(history_rows)]
            def checkpoint():
                current = self._row(turn_id)
                if current is None or current["stage"] != "responding": return False
                return self.authorized(identity, row["profile_id"])
            reply = self.runtime.run(row["profile_id"], history, row["text"], identity, checkpoint)
            if not isinstance(reply, str) or not reply.strip(): raise RuntimeError("empty_reply")
            if len(reply.encode("utf-8")) > 65536: raise RuntimeError("reply_too_large")
            if not self.authorized(identity, row["profile_id"]):
                self._set(turn_id, "failed", error="authorization_revoked"); return
            with db:
                db.execute("BEGIN IMMEDIATE")
                current = self._row(turn_id)
                if current["stage"] == "cancel_requested":
                    self._update(db, turn_id, "cancelled", error=None)
                elif current["stage"] == "responding":
                    db.execute("UPDATE pet_turns SET stage='completed',reply_text=?,revision=revision+1,updated_at=? "
                               "WHERE id=? AND stage='responding'",
                               (reply, __import__('time').time(), turn_id))
        except RuntimeUnavailable:
            self._set(turn_id, "failed", error="runtime_unavailable")
        except RuntimeStopped:
            current = self._row(turn_id)
            if current and current["stage"] not in TERMINAL:
                error = "cancelled" if current["stage"] == "cancel_requested" else "authorization_revoked"
                self._set(turn_id, "cancelled" if error == "cancelled" else "failed", error=None if error == "cancelled" else error)
        except RuntimeOutcomeUnknown:
            current = self._row(turn_id)
            if current and current["stage"] not in TERMINAL:
                self._set(turn_id, "unknown", error="provider_outcome_unknown")
        except Exception:
            current = self._row(turn_id)
            if current and current["stage"] not in TERMINAL:
                self._set(turn_id, "failed", error="runtime_error")
        finally:
            self.store.close()
            with self._lock: self._queued.discard(turn_id)

    @staticmethod
    def _update(db, tid, stage, error=None):
        import time
        db.execute("UPDATE pet_turns SET stage=?,revision=revision+1,error=?,updated_at=? WHERE id=?",
                   (stage, error, time.time(), tid))

    def _set(self, tid, stage, error=None):
        with self.store.db:
            if stage in TERMINAL:
                self.store.db.execute("UPDATE pet_turns SET stage=?,revision=revision+1,error=?,updated_at=? "
                                      "WHERE id=? AND stage IN ('accepted','responding','cancel_requested')",
                                      (stage, error, __import__('time').time(), tid))
            else:
                self._update(self.store.db, tid, stage, error)

    def get_turn(self, identity, turn_id):
        row = self._row(turn_id)
        if row is None or row["principal_id"] != identity["principal_id"] or row["credential_id"] != identity["credential_id"]: raise LookupError
        if not self.authorized(identity, row["profile_id"]): raise PermissionError
        return self.public(row)

    def get_request(self, identity, request_id):
        row = self.store.db.execute("SELECT * FROM pet_turns WHERE principal_id=? AND credential_id=? AND request_id=?",
                                    (identity["principal_id"], identity["credential_id"], request_id)).fetchone()
        if row is None: raise LookupError
        if not self.authorized(identity, row["profile_id"]): raise PermissionError
        return self.public(row)

    def cancel(self, identity, turn_id):
        self.get_turn(identity, turn_id)
        with self.store.db:
            now = __import__('time').time()
            db = self.store.db
            db.execute("UPDATE pet_turns SET stage='cancelled',revision=revision+1,updated_at=? "
                       "WHERE id=? AND stage='accepted'", (now, turn_id))
            db.execute("UPDATE pet_turns SET stage='cancel_requested',revision=revision+1,updated_at=? "
                       "WHERE id=? AND stage='responding'", (now, turn_id))
        return self.get_turn(identity, turn_id)
