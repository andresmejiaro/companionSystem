# Access control design (NOT enforced yet)

**Implementation status:** the storage/service foundation for this model
exists in `profile_os/access.py` (principals, hashed credentials, grants,
`allowed()`, `authenticate_secret()` — including `profile_id=None` global
grants and `profile_id="*"` all-profiles grants).

**Enforcement is partial:** with `PROFILE_OS_AUTH_ENABLED=1`, the dynamic-
store lifecycle endpoints (approve/reject/archive) require a bearer
credential whose principal holds `stores:approve` for the route's profile
(401 for missing/invalid/expired/revoked credentials or disabled principals;
403 for authenticated principals without the grant). **All other endpoints
remain open**, and auth is disabled by default — full endpoint enforcement is
a later slice. Credentials belong to principals/clients; profile-scoped keys
remain explicitly not the design. Bootstrap the first admin credential
locally (never over HTTP) with
`python -m profile_os.bootstrap_admin --data-dir data --secret "$SECRET"`.

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

## Enforcement point (when implemented)

FastAPI middleware/dependency: credential → principal → grant lookup →
allow/deny per (profile_id, operation) before any handler runs. Grants and
credential hashes live in SQLite next to everything else; issuance and
revocation are audited like store lifecycle events.
