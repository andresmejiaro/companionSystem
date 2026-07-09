# Plan: agent self-enrollment with public-key auth

**Goal:** an existing companion (an agent running elsewhere) can connect,
register itself as a principal with its own keypair, create its profile
("role and space"), and then operate that profile day-to-day **without
per-action human approval** — while anything privileged still requires the
admin.

**Executor:** Sonnet. Work in order; each phase lands with tests before the
next. No LLM in tests. Keep the ACCESS_CONTROL.md stance: **profiles are
resources, principals are identities** — the agent becomes a *principal*
(new kind `agent`), and its profile is a resource it owns.

---

## Phase 1 — Public-key credentials in `access.py`

1. Add dependency `cryptography` (Ed25519 is not in stdlib) to
   `requirements.txt`. Use only
   `cryptography.hazmat.primitives.asymmetric.ed25519`.
2. Extend `access_credentials` with columns `kind TEXT NOT NULL DEFAULT
   'secret'` (`secret` | `ed25519`) and `public_key TEXT` (base64 raw
   32-byte Ed25519 public key). Migration: `ALTER TABLE ... ADD COLUMN` in
   `ACCESS_SCHEMA` idempotent style (check `PRAGMA table_info` first, or a
   small `_migrate()` in `AccessControl.__init__`).
3. New methods:
   - `add_public_key(principal_id, label, public_key_b64, expires_at=None)`
     — validates the key parses as Ed25519 before storing; rejects
     duplicates of the same key.
   - `authenticate_signature(key_id, message: bytes, signature: bytes)` —
     returns the principal dict iff the credential is unrevoked/unexpired,
     principal enabled, and the signature verifies. Same
     "never authorize when disabled/revoked/expired" semantics as
     `authenticate_secret()` (mirror its tests).

## Phase 2 — Signed-request auth in `api.py`

Scheme (documented in ACCESS_CONTROL.md):

```
Authorization: Signature key_id=<cred-id>,ts=<unix>,nonce=<hex>,sig=<b64>
signed message = f"{ts}\n{nonce}\n{METHOD}\n{PATH}\n{sha256(body).hexdigest()}"
```

- Reject if `|now - ts| > 120s` (clock-skew window) → 401.
- Replay protection: in-memory nonce cache keyed `(key_id, nonce)` with
  TTL = 2× skew window. In-memory is fine (single-process server); note it
  in the doc.
- The existing bearer path stays unchanged; the auth dependency tries
  `Bearer` first, then `Signature`. Both resolve to a principal, then the
  existing grant check (`allowed()`) applies identically. **No changes to
  the route → operation map.**
- Provide a tiny client helper `profile_os/sign.py` (`sign_request(private_key,
  method, path, body) -> header value`) used by tests and by companions.

## Phase 3 — `POST /profiles` with auto-owner grants

1. Implement profile creation in `Store` if missing (id, display name,
   base/role prompt bodies written to the per-profile prompt files, as
   seed.py does — reuse/extract seed's logic rather than duplicating).
2. Route `POST /profiles` requires global operation `create_profile`
   (grant with `profile_id=None`).
3. On success, **automatically grant the creating principal** the owner
   bundle on the new profile (this is already promised in
   ACCESS_CONTROL.md, just not implemented):
   `boot, remember, search, closeout, records:read, records:write,
   stores:propose, manage_profile`. Explicitly **not** granted:
   `stores:approve, manage_grants, credentials:manage, delete_profile,
   audit:read` — schema approval and permissions stay with the admin.
4. **Store auto-approval within a small budget:** when the proposing
   principal owns the profile (holds `manage_profile` on it), a proposed
   dynamic store is **auto-approved** — no admin step — provided:
   - the profile has fewer than **3** approved-or-pending stores already
     (env `PROFILE_OS_AUTO_STORE_LIMIT`, default 3), and
   - the schema has at most **12** fields (env
     `PROFILE_OS_AUTO_STORE_MAX_FIELDS`, default 12).
   Auto-approvals are recorded in `store_audit` as
   `approved_by="auto:<principal_id>"` so they're distinguishable from
   admin approvals. Proposals over budget fall back to the existing
   pending → admin approve/reject flow (not rejected — just queued).
   Implement in `dynstores.py` as a parameter on `propose()` or a
   `maybe_auto_approve()` step called from the route, keeping approval
   logic in one place.
5. Body: `{id, display_name, base_prompt, role_prompt}`; validate id slug
   (`[a-z0-9_-]{1,64}`), 409 on existing profile. Audit the creation
   (reuse `store_audit`-style logging or add `access_audit` if trivial).
6. Optional cap: env `PROFILE_OS_MAX_PROFILES_PER_PRINCIPAL` (default 10)
   to stop a runaway agent from mass-creating profiles.

## Phase 4 — Enrollment flow (the "no approval for basics" part)

Admin approves **once** by minting an invite; everything after is
self-service within the invite's bounds.

1. New table `access_invites`: `id, token_hash (pbkdf2, reuse
   hash_secret), kind ('agent'), display_name_hint, created_at,
   expires_at, used_at, created_by_principal`.
2. Admin CLI `python -m profile_os.mint_invite --data-dir data
   [--expires-hours 24]` → prints one-time token. (CLI, not HTTP, like
   `bootstrap_admin` — invites are minted locally.)
3. Public route `POST /enroll` (no auth, like `/health`):
   body `{invite_token, display_name, public_key}`. Atomically:
   - verify + consume the invite (single-use; 410 if used/expired),
   - create principal `kind='agent'`,
   - register the Ed25519 key,
   - grant global `create_profile`,
   - return `{principal_id, key_id}`.
4. Add `agent` to `PRINCIPAL_KINDS`.
5. Rate-limit `/enroll` naively (in-memory, e.g. 5/min) since it's
   unauthenticated.

## Phase 5 — Docs, bridge, tests

- Update ACCESS_CONTROL.md (signature scheme, `agent` kind, invite flow,
  owner bundle) and API.md (`POST /profiles`, `POST /enroll`).
- Update `mcp_server.py` / `bridge.py` so a companion connecting through
  the bridge can use a keypair (load private key from env/file and sign).
- Tests (pytest, no network beyond TestClient):
  - sign/verify round-trip; tampered body/path/ts → 401; replayed nonce →
    401; expired ts → 401; revoked key → 401.
  - enroll happy path; reused invite → 410; expired invite → 410.
  - enrolled agent: creates profile → 201, immediately boots/remembers/
    searches/closes-out it **with no further grants**; first 3 store
    proposals (≤12 fields) auto-approve and accept record writes
    immediately; a 4th store or a 13-field schema goes to pending; cannot
    explicitly call `stores:approve` (403); cannot touch another profile
    (403); cannot create a second principal or grant itself anything.
  - existing bearer-auth test suite still green.

## End-to-end acceptance

```
1. admin: python -m profile_os.mint_invite            → token
2. agent: POST /enroll {token, name, pubkey}          → principal
3. agent: POST /profiles {id:"rumbo", ...} (signed)   → 201 + owner grants
4. agent: boot / remember / search / closeout — all work, zero admin
   involvement; proposes 2 stores (≤12 fields) → auto-approved, writes
   records immediately
5. agent proposes a 4th store (or an oversized one) → pending; admin
   approves it the old way
6. steal the invite token after use → 410; replay a request → 401
```
