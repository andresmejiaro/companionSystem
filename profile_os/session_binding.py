"""Session-bound companion locking.

Binds each MCP conversation to the companion it summoned and blocks
cross-profile writes (and, when the reads gate is on, cross-profile reads)
from that conversation. The binding key is a *conversation fingerprint*
carried below the model's view: ChatGPT's ``x-openai-session`` header or the
server-minted ``Mcp-Session-Id`` echoed by spec-compliant clients. The model
can neither read nor forge either, so it cannot re-target a write by editing
a tool argument. See SESSION_BINDING_PLAN.md for the full design and the wire
probe that discovered ``x-openai-session``.

Enforcement fails open: a request with no fingerprint, or a fingerprint with
no binding yet, is allowed and logged. An undocumented header disappearing
must never brick the connector; the ceiling for deliberate intent is
detection (the audit log), not prevention.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass

# Tools that write into a target profile's stores, files, records, prompt, or
# session state. A conversation bound to X may not invoke these against Y.
MUTATION_TOOLS = frozenset({
    "remember",
    "update_memory",
    "forget",
    "add_records",
    "update_record",
    "delete_record",
    "write_file",
    "delete_file",
    "update_ironsworn_sheet",
    "propose_prompt_edit",
    "propose_store",
    "closeout",
    "exam_attempt",
})

# Tools that read a target profile's private data. Blocked cross-profile only
# while the reads gate is on (a TOTP-gated admin switch can lift it for an
# audit without a redeploy).
READ_TOOLS = frozenset({
    "get_record",
    "query_records",
    "list_stores",
    "list_files",
    "read_file",
    "search_memories",
    "search_context",
    "read_inbox",
    "get_ironsworn_resource",
    "exam_review",
})

# send_message is the sanctioned escape hatch (reaching another companion) and
# is deliberately absent from both sets, as are the non-profile-scoped tools
# (discover_companions, summon_companion). Anything not listed is unguarded.


def classify_tool(name: str) -> str | None:
    """Return 'mutation', 'read', or None (unguarded) for a tool name."""
    if name in MUTATION_TOOLS:
        return "mutation"
    if name in READ_TOOLS:
        return "read"
    return None


@dataclass(frozen=True)
class Decision:
    action: str          # "allow" or "block"
    kind: str | None     # "mutation", "read", or None
    bound_profile: str | None
    reason: str          # audit label: allow / block / unbound / no-fingerprint / unguarded


class SessionBindingStore:
    """SQLite-backed conversation bindings + append-only audit.

    Kept on its own persistent file, deliberately separate from the closeout
    ledger: bindings are long-lived security state, not 30-minute reservations,
    and must not inherit the closeout DB's ephemeral default path.
    """

    def __init__(self, state_file: str, *, default_strict: bool = True):
        import os
        parent = os.path.dirname(state_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._connection = sqlite3.connect(state_file, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS bindings (
                fingerprint TEXT PRIMARY KEY,
                profile_id  TEXT NOT NULL,
                bound_at    INTEGER NOT NULL,
                last_seen   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_audit (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             INTEGER NOT NULL,
                fingerprint    TEXT,
                subject_hash   TEXT,
                tool           TEXT,
                target_profile TEXT,
                decision       TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gate_settings (
                key   TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            """
        )
        # One master switch. strict=1 (default) = the trust boundary is up:
        # one companion per session (no mid-session identity switch) and no
        # cross-profile reads. strict=0 = "revert to trusted": both relaxed at
        # once, for an audit or a deliberately trusted session.
        self._connection.execute(
            "INSERT OR IGNORE INTO gate_settings(key, value) VALUES ('strict_mode', ?)",
            (1 if default_strict else 0,),
        )
        self._connection.commit()
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> int:
        return int(time.time())

    def bind(self, fingerprint: str, profile_id: str) -> str | None:
        """Upsert fingerprint -> profile. Return the previous profile if this
        fingerprint was already bound to a *different* one (a rebind), else
        None. bound_at is preserved on re-summon of the same profile."""
        now = self._now()
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT profile_id FROM bindings WHERE fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                previous = row[0] if row else None
                if row is None:
                    connection.execute(
                        "INSERT INTO bindings(fingerprint, profile_id, bound_at, last_seen) "
                        "VALUES (?, ?, ?, ?)",
                        (fingerprint, profile_id, now, now),
                    )
                else:
                    connection.execute(
                        "UPDATE bindings SET profile_id=?, last_seen=? WHERE fingerprint=?",
                        (profile_id, now, fingerprint),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return previous if previous is not None and previous != profile_id else None

    def get(self, fingerprint: str) -> str | None:
        row = self._connection.execute(
            "SELECT profile_id FROM bindings WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        return row[0] if row else None

    def touch(self, fingerprint: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE bindings SET last_seen=? WHERE fingerprint=?",
                (self._now(), fingerprint),
            )
            self._connection.commit()

    def strict_mode(self) -> bool:
        """True = trust boundary up (one companion per session, no cross-profile
        reads). False = reverted to trusted (both relaxed)."""
        row = self._connection.execute(
            "SELECT value FROM gate_settings WHERE key='strict_mode'"
        ).fetchone()
        return bool(row[0]) if row else True

    def set_strict_mode(self, strict: bool) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO gate_settings(key, value) VALUES ('strict_mode', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (1 if strict else 0,),
            )
            self._connection.commit()

    def audit(self, *, fingerprint: str | None, subject_hash: str | None,
              tool: str | None, target_profile: str | None, decision: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO session_audit(ts, fingerprint, subject_hash, tool, "
                "target_profile, decision) VALUES (?, ?, ?, ?, ?, ?)",
                (self._now(), fingerprint, subject_hash, tool, target_profile, decision),
            )
            self._connection.commit()

    def prune(self, older_than_seconds: int = 24 * 60 * 60) -> int:
        cutoff = self._now() - older_than_seconds
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM bindings WHERE last_seen < ?", (cutoff,)
            )
            self._connection.commit()
            return cursor.rowcount

    def evaluate(self, fingerprint: str | None, tool: str,
                 target_profile: str | None) -> Decision:
        """Decide whether a tool call is allowed. Pure of side effects except
        reading gate state; the caller records the audit row with the subject."""
        kind = classify_tool(tool)
        if kind is None:
            return Decision("allow", None, None, "unguarded")
        if not target_profile or target_profile == "-":
            return Decision("allow", kind, None, "no-target")
        if fingerprint is None:
            return Decision("allow", kind, None, "no-fingerprint")
        bound = self.get(fingerprint)
        if bound is None:
            return Decision("allow", kind, None, "unbound")
        if bound == target_profile:
            return Decision("allow", kind, bound, "allow")
        if kind == "read" and not self.strict_mode():
            return Decision("allow", kind, bound, "reads-open")
        return Decision("block", kind, bound, "block")

    def switch_blocked(self, fingerprint: str | None, target_profile: str | None) -> str | None:
        """For summon_companion: return the currently-bound profile if switching
        to `target_profile` must be blocked (one companion per session), else
        None. First summon, same-companion re-summon, no fingerprint, and
        trusted mode all return None (allowed)."""
        if fingerprint is None or not target_profile:
            return None
        if not self.strict_mode():
            return None
        bound = self.get(fingerprint)
        if bound is None or bound == target_profile:
            return None
        return bound
