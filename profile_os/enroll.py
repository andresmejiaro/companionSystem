"""Agent self-enrollment: single-use invites minted locally by the admin.

Flow (see PLAN_AGENT_ENROLLMENT.md and ACCESS_CONTROL.md):

    1. admin (local CLI, never HTTP): python -m profile_os.mint_invite
    2. agent: POST /enroll {invite_token, display_name, public_key} (no auth)
       -> creates an `agent` principal, registers its Ed25519 key, grants
          global `create_profile`, and consumes the invite.

Invite tokens are hashed with the same PBKDF2 helper used for bearer
secrets; the plaintext token is only ever returned once, at mint time.
"""

from __future__ import annotations

import secrets as _secrets
import time
import uuid

from .access import AccessControl, AccessError, hash_secret, verify_secret

INVITE_KINDS = {"agent"}
DEFAULT_INVITE_EXPIRES_HOURS = 24


class EnrollmentError(Exception):
    """Base class; api.py maps subclasses to specific HTTP statuses."""


class InviteInvalid(EnrollmentError):
    """Unknown or malformed token."""


class InviteConsumed(EnrollmentError):
    """Already used or expired — 410 Gone."""


class Enrollment:
    def __init__(self, access: AccessControl):
        self._access = access

    @property
    def db(self):
        return self._access.db

    # -- minting (local CLI only) ------------------------------------------------

    def mint_invite(self, kind: str = "agent", display_name_hint: str = "",
                    expires_hours: float = DEFAULT_INVITE_EXPIRES_HOURS,
                    created_by_principal: str | None = None) -> tuple[str, str]:
        if kind not in INVITE_KINDS:
            raise AccessError(f"invalid invite kind {kind!r}; must be one of {sorted(INVITE_KINDS)}")
        token = _secrets.token_urlsafe(32)
        iid = str(uuid.uuid4())
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO access_invites (id, token_hash, kind, display_name_hint,"
                " created_at, expires_at, created_by_principal) VALUES (?,?,?,?,?,?,?)",
                (iid, hash_secret(token), kind, display_name_hint, now,
                 now + expires_hours * 3600, created_by_principal))
        return iid, token

    # -- consumption (public /enroll route) --------------------------------------

    def enroll(self, invite_token: str, display_name: str, public_key_b64: str) -> dict:
        if not invite_token:
            raise InviteInvalid("invite_token is required")
        if not display_name or not display_name.strip():
            raise InviteInvalid("display_name is required")
        row = self._match_invite(invite_token)
        if row is None:
            raise InviteInvalid("invalid invite token")
        now = time.time()
        if row["used_at"] is not None:
            raise InviteConsumed("invite already used")
        if row["expires_at"] <= now:
            raise InviteConsumed("invite expired")

        principal = self._access.create_principal("agent", display_name)
        credential = self._access.add_public_key(principal["id"], "enrollment key",
                                                  public_key_b64)
        self._access.grant(principal["id"], "create_profile", profile_id=None)

        with self.db:
            # Re-check + mark used atomically to prevent a concurrent double-spend.
            cur = self.db.execute(
                "UPDATE access_invites SET used_at=? WHERE id=? AND used_at IS NULL"
                " AND expires_at > ?", (now, row["id"], now))
            if cur.rowcount == 0:
                raise InviteConsumed("invite already used")

        self._access.record_audit(principal["id"], "enrolled",
                                  f"invite={row['id']} display_name={display_name}")
        return {"principal_id": principal["id"], "key_id": credential["id"]}

    def _match_invite(self, token: str):
        """Linear scan over all invites (small table; mirrors authenticate_secret).

        Used/expired invites must still be matchable so a replayed token
        reports 410 (already consumed) rather than 401 (unknown token).
        """
        rows = self.db.execute("SELECT * FROM access_invites").fetchall()
        for row in rows:
            if verify_secret(token, row["token_hash"]):
                return row
        return None
