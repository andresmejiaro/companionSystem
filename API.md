# API / tool contract — slice zero

Base URL: `http://127.0.0.1:8000`. All bodies JSON. Errors:
`404 {"detail": "unknown profile: 'x'"}`, `422 {"detail": "malformed ..."}`.

The remote MCP server in `profile_os/mcp_server.py` exposes a Claude-facing
subset of these operations over `POST /mcp` and `GET /mcp`; see
[MCP_CONNECTOR.md](MCP_CONNECTOR.md).

## Profiles

- `GET /health` → `{"ok": true}`
- `GET /profiles` → list of profile objects
- `GET /profiles/{id}` → profile object:
  ```json
  {"id": "tara", "display_name": "Tara", "description": "...",
   "allowed_tools": ["log_food"], "memory_policy": {"kinds": ["fact"]},
   "closeout_rules": "...", "created_at": 1751640000.0}
  ```
- `POST /profiles` `{id, display_name, base_prompt, role_prompt}` → 201,
  profile object. Requires the global `create_profile` grant; the creating
  principal automatically receives the owner grant bundle on the new
  profile (see ACCESS_CONTROL.md). `id` must match `[a-z0-9_-]{1,64}`; 409
  if it already exists.
- `DELETE /profiles/{id}` → 204. Requires `delete_profile` on that profile.
  Permanently removes the profile row, compact state, memories, legacy
  domain records, dynamic stores/records/audit, prompt files, and revokes
  any grants scoped to that profile id. 404 if unknown.
- `POST /profiles/totp-create` `{id, display_name, base_prompt?, role_prompt?,
  totp_code}` → 201, profile object. Public at the transport layer — no
  bearer header — authenticated by a live TOTP code alone (verified
  against the single TOTP-enrolled admin), rate-limited (5/min per IP).
  For creating/migrating a companion from mobile without the admin
  secret. Same `id`/409/422 rules as `POST /profiles`. Also reachable as a
  form at `GET /create-profile` on the mcp service.
- `POST /enroll` `{invite_token, display_name, public_key}` → 201
  `{principal_id, key_id}`. Unauthenticated, single-use; consumes an invite
  minted locally via `python -m profile_os.mint_invite`. 410 if the invite
  is already used/expired, 401 for an unknown token. See "Agent
  self-enrollment" in ACCESS_CONTROL.md.

## Core lifecycle

- ★ `POST /profiles/{id}/boot` → everything needed to start a session:
  ```json
  {"profile": {...}, "base_prompt": "...", "role_prompt": "...",
   "compact_state": "No meals logged today.", "state_updated_at": 1751640000.0,
   "recent_memories": [ {memory event}, ... ]}
  ```
- `POST /profiles/{id}/session` — one-call **hydration packet** for a
  companion's first turn (MCP tool `start_session`): prompts, current compact
  state, `identity` (the `whoami` file content, or `null` without
  `identity:read`), a bounded boot-memory slice reduced to `kind` and
  `content`, and `server_time` (`{"unix": 1751640000.0,
  "iso": "2026-07-10T12:00:00+00:00"}`). It deliberately omits memory IDs,
  tags, timestamps, full history, and closeout archives. Requires the same
  `boot` grant.
- `POST /profiles/{id}/prompt` `{base_prompt?, role_prompt?}` → 201, a
  pending approval record. Requires `manage_profile` on that profile.
  Companions can propose an edit to their own prompts; it only takes effect
  once an admin approves it with a live TOTP code — see "TOTP-gated
  approvals" in ACCESS_CONTROL.md.
- `GET /approvals?profile_id=` → list pending approvals. Requires global
  `approvals:decide`.
- `POST /approvals/{id}/decide` `{"approve": true|false, "totp_code": "..."}`
  → the approval record. Approving requires a valid, unused TOTP code
  (401 without one); rejecting does not. 409 if already decided.
- `PUT /profiles/{id}/description` `{description}` → 200, updated profile.
  Requires `manage_profile`. Self-service, no approval — unlike prompt
  edits this is discovery metadata (surfaced via `GET /profiles`), not
  behavior, so it takes effect immediately.
- ★ `POST /profiles/{id}/memories` — remember. Body:
  `{"kind": "note|fact|decision|failure_scar|preference|observation",
    "content": "non-empty", "tags": ["optional"]}` → 201 + stored event
  (`id`, `profile_id`, `kind`, `content`, `tags`, `created_at`).
- ★ `GET /profiles/{id}/memories/search?q=<text>&limit=20` — case-insensitive
  substring match over content and tags, newest first.
- `PATCH /profiles/{id}/memories/{event_id}` `{kind?, content?, tags?}` →
  200, the updated event. Self-service — same `remember` grant, no admin
  approval — a companion may revise its own memory. At least one field
  required; 404 if the event doesn't exist under that profile; 422 for an
  invalid kind/empty content/non-string tags.
- `DELETE /profiles/{id}/memories/{event_id}` → 204. Self-service erase,
  same `remember` grant. 404 if the event doesn't exist under that profile.

## Inbox (companion-to-companion messages)

No approval, no backend/admin action — a companion can hand something to
another companion directly instead of a human copy-pasting between
conversations.

- `POST /profiles/{from_id}/messages` `{to_profile_id, content}` → 201, the
  stored message (`id`, `from_profile_id`, `to_profile_id`, `content`,
  `created_at`, `read_at`). Requires `remember` on `from_id` (the sender
  must control the profile it's sending as); 404 if either profile is
  unknown; 422 for empty content.
- `GET /profiles/{id}/inbox?unread_only=false&limit=50` → messages
  addressed to `id`, newest first. Requires `search` on `id`.
- `POST /profiles/{id}/inbox/{message_id}/read` → 200, the now-read
  message. Requires `search` on `id`; 404 if that message isn't in `id`'s
  inbox (including a message `id` itself sent — only the recipient can
  mark it read).

## File store (plain files on disk — scripts, notes)

Self-service like the inbox above: no approval, not a database table, and
never touches git (lives under the gitignored `data/` dir, one
`files/` folder per profile). For stashing a script or note that doesn't
belong as a structured dynamic-store record.

- `PUT /profiles/{id}/files/{filename}` `{content}` → 201, file metadata
  (`filename`, `size`, `updated_at`). Requires `remember` on `id`.
  Overwrites if it exists — this is scratch storage, not versioned.
  `filename` must match `[A-Za-z0-9._-]{1,128}` (no path separators, no
  `..`); max 256KB.
- `GET /profiles/{id}/files` → list of file metadata. Requires `search`.
- `GET /profiles/{id}/files/{filename}` → metadata + `content`. Requires
  `search`; 404 if it doesn't exist.
- `DELETE /profiles/{id}/files/{filename}` → 204. Requires `remember`; 404
  if it doesn't exist.

- ★ `POST /profiles/{id}/closeout` — end session. Body:
  `{"notes": "free text", "new_state": "non-empty compact state"}` → 201.
  Replaces compact state and appends to `closeouts.jsonl`.
  **Compaction boundary:** the backend does not summarize. The *caller*
  (the assistant/model running the session) produces `new_state`; the backend
  only validates it is non-empty and stores it verbatim. `notes` is an audit
  trail, never an input to state.

## Dynamic stores (slice two)

Profile-scoped durable structured data. Lifecycle: a profile **proposes** a
store + schema, the user/admin **approves or rejects** it, the backend
**enforces** the schema on every write. Dynamic stores are not memory events —
they hold validated structured records, not free-text session memory.

> ⚠️ By default auth is **disabled** and everything is open on localhost.
> With `PROFILE_OS_AUTH_ENABLED=1`, every endpoint except `/health` and
> `/demo` requires `Authorization: Bearer <secret>` resolving to a principal
> granted the route's operation for the route's profile (or `*`):
> missing/invalid/expired/revoked credential → 401, no grant → 403.
> Route → operation map in ACCESS_CONTROL.md.

Schema format (tiny subset, no dependencies):
`{"fields": {"<name>": {"type": "string|number|integer|boolean|date", "required": true|false}}}`
— fields required by default; `date` is a `YYYY-MM-DD` string; records may not
contain unknown fields.

- `POST /profiles/{id}/stores` — propose. Body:
  `{"name": "hotel_reservations", "purpose": "...", "proposed_by": "tara",
    "schema": {"fields": {...}}}` → 201. Pending proposals return
  `status: "pending"` plus `approval_id`; auto-approved owner proposals
  return `status: "approved"`.
- `GET /profiles/{id}/stores` → latest version of each store definition
- `GET /profiles/{id}/stores/{name}` → definition (id, name, version, purpose,
  proposed_by, schema, status, timestamps, rejection_reason)
- `POST /profiles/{id}/stores/{name}/approve` (admin) — pending → approved
- `POST /profiles/{id}/stores/{name}/reject` (admin) — body `{"reason": "..."}`
- `POST /profiles/{id}/stores/{name}/archive` (admin) — approved → archived
  (read-only; records stay queryable)
- ★ `POST /profiles/{id}/stores/{name}/records` — body `{"data": {...}}`.
  Only approved stores accept writes: pending/rejected/archived → 409;
  schema violation → 422.
- ★ `GET /profiles/{id}/stores/{name}/records?contains=&limit=50`
- `GET /profiles/{id}/stores/{name}/audit`, `GET /profiles/{id}/audit` —
  audit trail (proposed/approved/rejected/archived, with actor and detail)

Versioning: schemas are immutable. To change one, reject/archive the current
definition and re-propose the same name — version increments and the new
schema needs its own approval. No data migrations; old records keep their
`schema_version`.

## Domain stores (legacy, slice zero — schemaless; superseded by dynamic stores)

- `GET /profiles/{id}/domain` → list of store names, e.g. `["meals","products"]`
- ★ `GET /profiles/{id}/domain/{store}?contains=<text>&limit=50` → records:
  `{"id": "...", "store": "products", "data": {...}, "created_at": ...}`
- ★ `POST /profiles/{id}/domain/{store}` — body `{"data": {...non-empty...}}` → 201

## Guarantees

- All operations are profile-scoped; no cross-profile reads/writes exist.
- Memory operations can never modify prompt/identity files.
- Auth: opt-in via `PROFILE_OS_AUTH_ENABLED=1` (off by default for local
  use). Principal/client-based credentials with many-to-many grants over
  profiles and operations — not one key per profile. When enabled,
  `GET /profiles` is filtered to profiles the principal holds any grant on.
  See ACCESS_CONTROL.md.
