# Access control design (enforced on all routes)

**Implementation status:** the storage/service foundation for this model
exists in `profile_os/access.py` (principals, hashed credentials, grants,
`allowed()`, `authenticate_secret()`, `visible_profile_ids()` — including
`profile_id=None` global grants and `profile_id="*"` all-profiles grants).
Credentials come in two kinds: shared-secret (`secret_hash`, PBKDF2) and
Ed25519 public-key (`kind='ed25519'`, `public_key`) — see `add_public_key()`
and `authenticate_signature()`.

**Enforcement is complete for existing endpoints:** with
`PROFILE_OS_AUTH_ENABLED=1`, every route except `GET /health`, `GET /demo`,
and `POST /enroll` requires a credential (bearer secret or Ed25519 signature)
whose principal holds the route's operation grant for the route's profile
(401 for missing/invalid/expired/revoked credentials or disabled principals;
403 for authenticated principals without the grant). Auth is disabled by
default (open on localhost), and behavior with auth off is unchanged.
Credentials belong to principals/clients; profile-scoped keys remain
explicitly not the design. Bootstrap the first admin credential locally
(never over HTTP) with
`python -m profile_os.bootstrap_admin --data-dir data --secret "$SECRET"`.

## Signed-request auth (Ed25519)

An alternative to bearer secrets, for principals that hold a keypair instead
of (or in addition to) a shared secret — notably self-enrolled `agent`
principals (see "Agent self-enrollment" below).

```
Authorization: Signature key_id=<credential-id>,ts=<unix-seconds>,nonce=<hex>,sig=<base64>
signed message = f"{ts}\n{nonce}\n{METHOD}\n{PATH}\n{sha256(body).hexdigest()}"
```

`PATH` is the request path only (no query string). `api.py` tries `Bearer`
first, then `Signature`; both resolve to a principal, then the existing
grant check (`allowed()`) applies identically — no changes to the route →
operation map below.

## `start_session`: one-call bootstrap for a companion's first turn

`POST /profiles/{id}/session` (MCP tool `start_session`) is the agent's
hydration packet: `whoami` identity content (if the caller has
`identity:read`), current prompts and `compact_state`, a bounded boot-memory
slice reduced to semantic `kind`/`content`, and `server_time` (`unix` epoch
seconds + `iso` UTC timestamp). It does not inject storage IDs, tags,
timestamps, full memory history, or closeout archives. Those are retrieved
through the appropriate lookup tools only when useful. It is gated by the
same `boot` grant as the plain `boot`/`boot_profile` tool. Intended so a
connector's provider-side system prompt can shrink to "on your first
response, call `start_session`."

## TOTP-gated approvals ("edgy" actions)

Routine writes (`remember`, `closeout`, dynamic-store records) never need
extra confirmation. A small set of actions a companion shouldn't be able to
do unilaterally go through **propose → pending → human decision**, and
*approving* (not rejecting) requires a live 6-digit TOTP code from an
authenticator app (Google/Microsoft Authenticator, etc.) — companions can
propose but can never approve their own edits.

Currently covers: a companion editing its own `base_prompt`/`role_prompt`
(`POST /profiles/{id}/prompt`, requires `manage_profile` on that profile —
the same grant a profile owner already holds). More action kinds can reuse
the same `access_pending_approvals` table/`kind` field later (e.g. agent
enrollment approval) without a schema change.

**Not** gated this way: `PUT /profiles/{id}/description` — a companion's
one-line "what do I do" is discovery metadata (surfaced via
`list_profiles`, so other companions know who to message about what), not
identity/behavior, so it updates immediately with no propose/approve step.
This is the deliberate boundary: prompts shape *how a companion acts*;
description just answers *what should another companion ask it about*.

- Enroll the admin's authenticator app **locally, once**: `python -m
  profile_os.enroll_totp --data-dir data` prints an `otpauth://` URI to
  scan or paste manually, then `--confirm <code>` activates it. Unconfirmed
  secrets are refused by `verify_totp()`, so a half-finished enrollment
  can't silently gate approvals.
- `GET /approvals` (global `approvals:decide`) lists pending proposals.
- `POST /approvals/{id}/decide {"approve": true, "totp_code": "123456"}`
  applies the edit; `{"approve": false}` rejects it, no code required.
  Each TOTP code is single-use (a per-principal `last_used_counter`
  rejects any code from an already-consumed 30-second window — the same
  replay concern as the nonce cache above, on a longer clock).
- `python -m profile_os.bootstrap_admin` grants `approvals:decide` to the
  admin principal by default.

## TOTP-only approval links

The curl-based flow above requires the admin secret, which is deliberately
not something you'd carry on your phone. For approving a companion's
proposed prompt edit or dynamic-store schema — lower stakes than an OAuth
login, and something you might need to do away from a desktop — there's a
second, lighter path: a public link, TOTP code only, no admin secret.

- `propose_prompt_edit` (MCP tool / `POST /profiles/{id}/prompt`) returns
  an `approval_link` when `MCP_PUBLIC_BASE_URL` is set:
  `https://<host>/approvals/<id>`. A companion can hand this straight to
  the human.
- `propose_store` (MCP tool / `POST /profiles/{id}/stores`) returns an
  `approval_id` for pending schema proposals; the MCP tool response also
  includes `approval_link` when `MCP_PUBLIC_BASE_URL` is set. The link
  targets that exact schema proposal row, not just the store name.
- `GET /approvals/{id}` on the **mcp** service (public, no auth at the
  HTTP layer — same principle as the OAuth consent screen) renders the
  proposed prompt edit or schema and a form asking only for a 6-digit code.
- `POST /approvals/{id}` submits the decision. The mcp service calls the
  backend using its own bridge credential (holds `approvals:totp_decide`,
  a narrower operation than full `approvals:decide` — it can submit a
  decision but never bypasses the TOTP check). The backend then verifies
  the human-entered code against `find_totp_admin_principal_id()` — the
  single TOTP-enrolled principal holding global `approvals:decide` — since
  this deployment has exactly one admin, "whose TOTP" is unambiguous.
  Approving without a valid code still fails (401); rejecting needs none.
- Rate-limited (5/min per IP) on both the mcp-side page and the backend
  decide route.
- `bootstrap_bridge.py` grants `approvals:totp_decide` to the bridge
  principal by default, alongside `identity:read`.

## TOTP-only profile creation

Creating a profile normally requires the global `create_profile` grant —
fine at a desk with the admin secret, not something carried on a phone.
`POST /profiles/totp-create` (backend) / `GET+POST /create-profile` (mcp
service, public page) creates a profile with a live TOTP code alone, same
principle as the approval links above: verified against
`find_totp_admin_principal_id()`, rate-limited (5/min per IP), no owner
grants needed (the admin already covers every profile via its
wildcard-scoped grants, and the MCP bridge already covers every profile
via `PROFILE_OS_MCP_BACKEND_PROFILES=*`). Intended for creating or
migrating a companion away from a desktop.

## OAuth authorize consent screen

`POST /oauth/register` (dynamic client registration) is open by design —
that's how Claude.ai/ChatGPT auto-configure a connector from just a URL,
and it's required by the MCP spec. That also means **anyone** can register
their own client the same way. Without a human check somewhere in the
flow, `GET /oauth/authorize` would hand out a real access token to any
browser that hit it — client registration authenticates the *software*,
not the person clicking "approve."

So `GET /oauth/authorize` renders a login form (admin secret + live TOTP
code) instead of auto-issuing a code. `POST /oauth/authorize` verifies
those against the backend's `POST /admin/verify-totp` (requires
`approvals:decide`) before creating the code and redirecting back to the
connector. Wrong secret/code re-shows the form with an error, no code
issued; the endpoint is rate-limited (5/min per IP) against brute force.

This only happens once per connector setup, not per message: the issued
access token is valid for `MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS` (a personal
single-operator deployment can reasonably set this long — e.g. a year —
since re-authorizing means going through the TOTP screen again; there is
no refresh-token grant, only `authorization_code`).

`create_mcp_app(..., admin_verify=...)` accepts an injectable async
`(secret, totp_code) -> bool` for tests; production defaults to
`default_admin_verify`, which calls the backend over HTTP using
`PROFILE_OS_BRIDGE_BASE_URL`.

- Requests older or newer than 120s (clock skew) are rejected (401).
- Replay protection is an in-memory `(key_id, nonce)` cache with a
  240s TTL — fine for a single-process server, not durable across restarts.
- `profile_os/sign.py` provides `sign_request(private_key, key_id, method,
  path, body) -> header value` for clients/tests. `ToolBridge` (bridge.py)
  can use it in place of a bearer secret via
  `PROFILE_OS_BRIDGE_KEY_ID` / `PROFILE_OS_BRIDGE_PRIVATE_KEY`.

## Agent self-enrollment

An admin mints a single-use invite **locally, never over HTTP**:

```
python -m profile_os.mint_invite --data-dir data --expires-hours 24
```

The agent then calls the one public, unauthenticated route:

```
POST /enroll {"invite_token": "...", "display_name": "...", "public_key": "<base64 ed25519 pubkey>"}
-> {"principal_id": "...", "key_id": "..."}
```

This atomically: consumes the invite (single-use; replay/expiry → 410),
creates a principal of kind `agent`, registers its Ed25519 key, and grants
it the global `create_profile` operation — nothing else. `/enroll` is
naively rate-limited (5/min per client IP) since it is unauthenticated.

From there the agent is self-service within that one grant:

- `POST /profiles {id, display_name, base_prompt, role_prompt}` (signed)
  creates its own profile and **automatically grants the creating principal
  the owner bundle** on it: `boot, remember, search, closeout,
  records:read, records:write, stores:propose, manage_profile`. Explicitly
  *not* granted: `stores:approve, manage_grants, credentials:manage,
  delete_profile, audit:read` — schema approval and permissions stay with
  the admin. A principal is capped at `PROFILE_OS_MAX_PROFILES_PER_PRINCIPAL`
  profiles (default 10).
- **Store auto-approval within a budget:** when the proposing principal owns
  the profile (holds `manage_profile` on it), a proposed dynamic store is
  auto-approved with no admin step, provided the profile has fewer than
  `PROFILE_OS_AUTO_STORE_LIMIT` (default 3) approved-or-pending stores and
  the schema has at most `PROFILE_OS_AUTO_STORE_MAX_FIELDS` (default 12)
  fields. Auto-approvals are recorded in `store_audit` as
  `approved_by="auto:<principal_id>"`. Proposals over budget fall back to
  the normal pending → admin approve/reject flow.

An enrolled agent can never call `stores:approve`, touch another profile,
create a second principal, or grant itself anything beyond what enrollment
and self-created-profile ownership provide.

## Identity file ("quién soy" — drift prevention)

`GET /identity` (global `identity:read` grant, not profile-scoped) serves a
canonical, human-authored "who am I talking to" document that overrides
whatever a companion's memory says on conflict. Exposed as the MCP tool
`whoami`.

- The file itself (`PROFILE_OS_IDENTITY_FILE` env var, an absolute path) is
  **never committed to the repo** — it typically contains health, family,
  and other sensitive personal details. It lives on the VPS only, outside
  the git-tracked tree (see `.gitignore`'s `quien_soy*` / `*identity*.md`
  patterns as a backstop in case it's ever placed inside the repo dir by
  mistake).
- Companions never write to it — only the admin edits it directly on the
  server. There is deliberately no write route.
- Access is gated like everything else: a principal needs the global
  `identity:read` grant. `bootstrap_bridge.py` grants it to the MCP bridge
  principal by default, so any connector authenticated through that bridge
  (Claude via OAuth, ChatGPT via OAuth or connector token) can call
  `whoami` — the existing MCP-layer auth (OAuth/connector token) plus this
  backend grant is the access control; there is no separate auth system
  for this one endpoint.

### Route → operation map

| Route | Operation |
|---|---|
| `GET /health`, `GET /demo`, `POST /enroll` | public |
| `GET /identity` | global `identity:read` |
| `POST /profiles/{id}/session` | `boot` (same grant as normal boot) |
| `POST /profiles/{id}/prompt` (propose) | `manage_profile` |
| `GET /approvals`, `POST /approvals/{id}/decide` | global `approvals:decide` |
| `GET /profiles` | authenticated; filtered to profiles with any active grant (`*` sees all) |
| `POST /profiles` | global `create_profile`; auto-grants owner bundle on the new profile |
| `GET /profiles/{id}`, `POST /profiles/{id}/boot` | `boot` |
| `POST /profiles/{id}/memories` | `remember` |
| `PATCH \| DELETE /profiles/{id}/memories/{event_id}` | `remember` (self-service edit/erase, no admin approval) |
| `POST /profiles/{from_id}/messages` | `remember` on `from_id` (self-service, no admin approval) |
| `GET /profiles/{id}/inbox`, `POST /profiles/{id}/inbox/{message_id}/read` | `search` |
| `PUT \| DELETE /profiles/{id}/files/{filename}` | `remember` (self-service, no admin approval) |
| `GET /profiles/{id}/files`, `GET /profiles/{id}/files/{filename}` | `search` |
| `PUT /profiles/{id}/description` | `manage_profile` (self-service, no TOTP — discovery metadata, not behavior) |
| `GET /profiles/{id}/memories/search` | `search` |
| `POST /profiles/{id}/closeout` | `closeout` |
| `GET /profiles/{id}/domain`, `GET /profiles/{id}/domain/{store}` | `records:read` |
| `POST /profiles/{id}/domain/{store}` | `records:write` |
| `POST /profiles/{id}/stores` | `stores:propose` |
| `GET /profiles/{id}/stores`, `GET /profiles/{id}/stores/{name}` | `records:read` |
| `POST …/stores/{name}/approve\|reject\|archive` | `stores:approve` |
| `GET …/stores/{name}/records` | `records:read` |
| `POST …/stores/{name}/records` | `records:write` |
| `GET …/stores/{name}/audit`, `GET /profiles/{id}/audit` | `audit:read` |

## Core stance: Assistant Profiles are resources, not principals

An Assistant Profile (tara, sidra, …) is a configuration-and-data bundle —
a thing that gets accessed. It does not log in, does not own credentials,
and is not an identity. **Credentials belong to principals; profiles are
what grants point at.** Consequently, "profile-scoped API keys" (one key per
profile) is *not* the design: keys would multiply with profiles and say
nothing about who is calling.

## Principal types

| Principal | Example | Typical needs |
|---|---|---|
| **Human/user** | the owner on their laptop/phone | list/access their profiles, run sessions, maybe create profiles |
| **App/client** | a health app, the web console | operate a fixed set of profiles on the user's behalf |
| **External model-runtime / tool bridge** | an MCP/HTTP adapter used by a Claude.ai / ChatGPT / Gemini-hosted assistant | narrow operational access to the profile(s) it fronts |
| **Admin/operator** | the person running the backend | manage profiles, approve schemas, manage credentials, read audit logs |

## Authorization model

- **Grants are many-to-many** over (principal, profile, operations).
  One principal ↔ many profiles; one profile ↔ many principals.
- A credential is scoped by: **allowed operations**, **allowed profiles**,
  and an **optional expiration**.
- Creating a new profile does **not** require minting a new key. If a
  principal holds `create_profile`, it may create profiles and automatically
  receives owner/admin grants over each profile it creates.
- Deleting or modifying profile definitions requires explicit
  profile-management permission (`delete_profile`, `manage_profile`) —
  never implied by data access.
- Granting/revoking permissions requires separate permission-management
  authority; no operational credential gets it by default.

Operation vocabulary (initial): `boot`, `remember`, `search`, `closeout`,
`records:read`, `records:write`, `stores:propose`, `stores:approve`
(covers reject/archive), `create_profile`, `delete_profile`,
`manage_profile`, `manage_grants`, `audit:read`, `credentials:manage`.

## Required scenarios

### 1. External hosted assistant (tool bridge)
A Claude.ai/ChatGPT/Gemini-hosted assistant reaches this backend through an
MCP/HTTP tool bridge. **The bridge owns the credential, not the model** —
the model never sees the secret; the runtime injects it per call. The
credential may be scoped to a single profile (only tara) or to several
profiles when the platform forces one shared bridge for multiple hosted
assistants (e.g. tara + sidra). Scope stays operational: boot/remember/
search/closeout/records — no schema approval, no profile management.

### 2. App/client
A health app holds **one** credential granting
`boot, search, remember, closeout, records:read, records:write` over
**tara and rumbo**. It does not need a key per profile, and it cannot delete
profiles or approve schemas unless those grants are explicitly added.

### 3. Human user
A human authenticates through an app/client (the app holds the session; the
user is the principal). Depending on grants, the user may: list and access
their allowed profiles; create profiles (`create_profile`, receiving owner
grants on each new profile); delete profiles (`delete_profile`); grant or
revoke permissions (`manage_grants`); approve/reject/archive dynamic stores
(`stores:approve`).

### 4. Admin/operator
Holds profile management, `stores:approve`, `credentials:manage` (issue and
revoke), and `audit:read` across profiles. Admin is a role assembled from
grants, not a hardcoded superuser path — though a break-glass root credential
may exist for the single-operator deployment.

### 5. Temporary external-agent/session access
A model/tool session can receive a narrow, short-lived capability grant:
limited profiles, limited operations, an expiration time, and never
permission management, profile deletion, or schema approval unless
explicitly intended. Expiry makes leaked session credentials time-bounded.

## Example grants

| Principal | Profiles | Operations | Expiry |
|---|---|---|---|
| owner (human) | all | all, incl. create/delete profile, manage_grants, stores:approve | none |
| health mobile app | tara, rumbo | boot, search, remember, closeout, records:read, records:write | none |
| Claude Tara bridge | tara | boot, search, remember, closeout, records:read, records:write, stores:propose | none |
| shared external bridge | tara, sidra | same operational set | none |
| admin operator | all | manage_profile, delete_profile, stores:approve, manage_grants, credentials:manage, audit:read | none |
| temp session grant | tara | search, records:read | +24h |

## Enforcement point

Implemented in `profile_os/api.py`: each route resolves the bearer
credential to a principal (`_authenticate`) and checks the route's
operation grant (`_require`) before the handler body runs. Grants and
credential hashes live in SQLite next to everything else.
