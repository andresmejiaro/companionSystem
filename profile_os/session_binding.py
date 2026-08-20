"""Session-bound companion locking — token only.

Locks each MCP conversation to the companion it summoned and blocks
cross-profile writes (and, in strict mode, cross-profile reads and mid-session
companion switches). The binding is carried by a single **session token**:
minted on ``summon_companion`` and returned to the model, which must echo it as
the ``session_token`` argument on every subsequent call.

Deliberately the *simple* shape — one mechanism, every surface, no transport
headers. We do not read x-openai-session, x-conv-id, or Mcp-Session-Id: those
each brought a surprise (claude.ai reused one Mcp-Session-Id across
conversations; the others need unverified client behaviour), and the whole
point here is no surprises. The cost is honesty about what this is: the token
is visible to the model, so it is ceremony, not a cryptographic wall — a
determined model can copy its own token. The goal is to make *accidental*
cross-companion access impossible and *deliberate* crossing conspicuous (audit
log), on every client, with nothing to install.

Trust posture (one TOTP-gated switch, ``strict_mode``, default on):
- **strict**: a guarded call with no valid token is BLOCKED (fail-closed —
  "summon first and carry the token"); switching companions mid-session is
  blocked; cross-profile reads are blocked.
- **trusted** ("revert to trusted"): guarded calls with no token fall open
  (advisory), switching is allowed, cross-profile reads are allowed.
  Cross-profile *writes* stay blocked whenever a token identifies a session.

See SESSION_BINDING_PLAN.md.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
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
# in strict mode (the trust switch lifts it for an audit without a redeploy).
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
class CallDecision:
    action: str          # "allow" | "block"
    reason: str          # unguarded|no-target|advisory|no-session|allow|reads-open|cross
    kind: str | None     # "mutation" | "read" | None
    bound_profile: str | None


@dataclass(frozen=True)
class SummonDecision:
    action: str          # "allow" | "block"
    reason: str          # bind|resummon|rebind|switch-blocked
    token: str | None    # raw token to hand back to the model (on allow)
    bound_profile: str | None  # currently-bound profile (on switch-blocked)


class SessionBindingStore:
    """SQLite-backed session tokens + append-only audit. Own persistent file,
    separate from the closeout ledger (long-lived security state, not
    30-minute reservations)."""

    def __init__(self, state_file: str, *, default_strict: bool = True,
                 fingerprint_key: str | None = None):
        # Tokens are secrets; this DB lands in nightly backups. Store only a
        # keyed HMAC digest — enough to match a session, useless if the file
        # leaks, and not reversible over the finite token space.
        self._fp_key = fingerprint_key
        parent = os.path.dirname(state_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._connection = sqlite3.connect(state_file, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_digest TEXT PRIMARY KEY,
                profile_id   TEXT NOT NULL,
                bound_at     INTEGER NOT NULL,
                last_seen    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             INTEGER NOT NULL,
                token_digest   TEXT,
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
        # One master switch. strict=1 (default) = trust boundary up. strict=0 =
        # "revert to trusted" (one-companion lock and reads wall relaxed).
        self._connection.execute(
            "INSERT OR IGNORE INTO gate_settings(key, value) VALUES ('strict_mode', ?)",
            (1 if default_strict else 0,),
        )
        self._connection.commit()
        self._lock = threading.Lock()

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _now() -> int:
        return int(time.time())

    def _digest(self, value: str | None) -> str | None:
        if value is None:
            return None
        if self._fp_key:
            return hmac.new(self._fp_key.encode(), value.encode(),
                            hashlib.sha256).hexdigest()
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _new_token() -> str:
        return "st_" + secrets.token_urlsafe(24)

    def _by_token(self, token: str | None):
        if not token:
            return None
        return self._connection.execute(
            "SELECT profile_id FROM sessions WHERE token_digest=?",
            (self._digest(token),),
        ).fetchone()

    # --- state ----------------------------------------------------------

    def strict_mode(self) -> bool:
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

    def _mint(self, profile: str) -> str:
        token = self._new_token()
        now = self._now()
        with self._lock:
            self._connection.execute(
                "INSERT INTO sessions(token_digest, profile_id, bound_at, last_seen) "
                "VALUES (?, ?, ?, ?)",
                (self._digest(token), profile, now, now),
            )
            self._connection.commit()
        return token

    def _rebind(self, token: str, profile: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE sessions SET profile_id=?, last_seen=? WHERE token_digest=?",
                (profile, self._now(), self._digest(token)),
            )
            self._connection.commit()

    def _touch(self, token: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE sessions SET last_seen=? WHERE token_digest=?",
                (self._now(), self._digest(token)),
            )
            self._connection.commit()

    def audit(self, *, token: str | None, subject_hash: str | None,
              tool: str | None, target_profile: str | None, decision: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO session_events(ts, token_digest, subject_hash, tool, "
                "target_profile, decision) VALUES (?, ?, ?, ?, ?, ?)",
                (self._now(), self._digest(token), subject_hash, tool,
                 target_profile, decision),
            )
            self._connection.commit()

    def prune(self, older_than_seconds: int = 24 * 60 * 60) -> int:
        cutoff = self._now() - older_than_seconds
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM sessions WHERE last_seen < ?", (cutoff,)
            )
            self._connection.commit()
            return cursor.rowcount

    # --- policy ---------------------------------------------------------

    def evaluate_call(self, token: str | None, tool: str,
                      target_profile: str | None) -> CallDecision:
        """Decide a (non-summon) tool call. Side-effect-free except touch on
        allow; the caller writes the audit row."""
        kind = classify_tool(tool)
        if kind is None:
            return CallDecision("allow", "unguarded", None, None)
        if not target_profile or target_profile == "-":
            return CallDecision("allow", "no-target", kind, None)

        row = self._by_token(token)
        if row is None:
            if not self.strict_mode():
                return CallDecision("allow", "advisory", kind, None)
            return CallDecision("block", "no-session", kind, None)

        profile = row[0]
        if profile == target_profile:
            self._touch(token)
            return CallDecision("allow", "allow", kind, profile)
        if kind == "read" and not self.strict_mode():
            return CallDecision("allow", "reads-open", kind, profile)
        return CallDecision("block", "cross", kind, profile)

    def evaluate_summon(self, token: str | None,
                        target_profile: str) -> SummonDecision:
        """Decide + apply a summon: mint on a fresh session, re-summon the same
        companion idempotently, block (strict) or rebind (trusted) a switch."""
        row = self._by_token(token)
        if row is None:
            # No/unknown token: a new session.
            return SummonDecision("allow", "bind", self._mint(target_profile), None)
        profile = row[0]
        assert token is not None
        if profile == target_profile:
            self._touch(token)
            return SummonDecision("allow", "resummon", token, None)
        if self.strict_mode():
            return SummonDecision("block", "switch-blocked", None, profile)
        self._rebind(token, target_profile)
        return SummonDecision("allow", "rebind", token, profile)
