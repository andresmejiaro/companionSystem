# Tamagochi backend contract

Status: implementation in progress, 2026-10-17. Dedicated registered device
credentials and durable text-conversation lifecycle routes are implemented
locally. See `TAMAGOCHI_BACKEND_SETUP.md` for the supported checkout behavior.
The provider adapter remains under review; the ordinary application has no
configured model runtime. Voice routes are still proposals. Deployment and
real provider behavior remain unverified.

Latest requirement: AI execution must use Codex or agy account access, with no
provider API key. Environment activation of the Responses prototype has been
removed. A CLI runtime adapter is still pending; ordinary app text/voice flags
remain false even if legacy provider settings exist. Prior Responses opt-in
instructions are historical and superseded.

The source survey and proposed contract below describe the original design
baseline. Their statements that pet authentication, text conversation/turn
routes and durable request IDs are absent have been superseded by
`pet_devices.py`, `pet_conversations.py`, and `pet_api.py`. They must not be read
as the current implementation inventory. Speech retry, audio retrieval and
voice submission below remain unimplemented.

## What exists in this checkout

- `profile_os/api.py:create_app()` exposes profile/data operations, including
  `POST /profiles/{id}/session` for a hydration packet. The root explicitly
  redirects to `/directory` “without restoring a conversation surface” (see
  `api.py`, lines 558–565, 870–967). There is no `assistant_server.py` or
  `openai_assistant.py` in this checkout.
- `profile_os/mcp_server.py` exposes MCP tools including `summon_companion`,
  memory, files, records, and closeout. `summon_companion` calls
  `ToolBridge.start_session()` to fetch context; the MCP service does not call
  a model or own chat turns (`mcp_server.py`, lines 1123–1200, 1298–1328).
  `profile_os/openai_smoke.py` is a local smoke runner, while
  `profile_os/harness.py` is scripted rehearsal; neither is the deployed
  conversation runtime. `profile_os/adapters.py` contains only a deterministic
  fake.
- Durable continuity is profile-owned, not a transcript API. Session hydration
  combines canonical prompts/current compact handoff and bounded memories,
  recent closeout exchanges, and recent session chronology
  (`api.py`, lines 870–967; `storage.py`, lines 516–559, 843–940). Memory
  events are persisted separately (`storage.py`, lines 586 onward). Full
  provider conversation transcripts, conversation IDs, and resumable turn
  records are not represented by these operations. A client can preserve its
  own transcript, but the backend offers no current contract to resume it or
  claim that another surface shares it.
- Authentication is configurable and **disabled by default** unless
  `PROFILE_OS_AUTH_ENABLED=1` (`api.py`, lines 336–345, 473–506). With it on,
  routes authenticate a principal by bearer secret or signed Ed25519 request,
  then check a per-profile operation grant (`api.py`, lines 473–526;
  `access.py`, lines 197–288, 477–559). Expired/revoked credentials and grants
  and disabled principals stop authorizing. Credential and grant service
  methods support expiry/revocation, but this checkout has no HTTP client
  credential provisioning/revocation API; credential setup is administrative
  (see `ACCESS_CONTROL.md`).
- MCP OAuth dynamically registers public clients, requires PKCE S256, and
  issues a signed bearer token after admin-secret plus TOTP consent. That
  bearer is scoped to the MCP
  resource/scope, with configurable expiry, but there is no per-device token
  revocation endpoint or profile/operation grant embedded in it; MCP uses its
  configured backend bridge credential. It is not the Tamagochi device
  credential flow (`mcp_server.py`, lines 858–918, 1622–1699, 1890–2131).
- Operation grants can deny calls such as memory writes at the API boundary.
  The existing MCP registry, however, advertises a broad companion tool set;
  there is no generic server-enforced “read-only tool mode” that filters that
  set. `PROFILE_OS_READ_ONLY=1` blocks application writes in middleware, but
  it is a whole-service setting and is not a voice-surface policy
  (`api.py`, lines 40–88, 419–440). `allowed_tools` is profile metadata, not
  proof of per-call server enforcement. Treat grants as the restriction
  mechanism and verify all runtime calls use them.
- Approval support is for specific durable/admin changes, such as prompt edits
  and store/project proposals, with a pending status and human/TOTP decision
  (`api.py`, lines 1304–1334, 1346–1437; `access.py`, lines 388–475).
  There is no conversation tool approval handoff, turn approval state, or
  voice confirmation API. A spoken “yes” must not be treated as authorization.
- No voice transcription/synthesis adapters, audio endpoints, turn status,
  asynchronous job records, cancellation endpoint, or voice request
  idempotency key are present in the inspected application. MCP JSON-RPC
  request IDs correlate protocol requests only; they are not durable operation
  IDs.

## Proposed `/pet/v1` contract (not implemented)

Keep this as a small adapter inside the authenticated Companions boundary; do
not add another identity or memory database. `GET /pet/v1/capabilities` returns
contract version, permitted canonical profile IDs and limits. Until each
operation is implemented and deployed, advertise `text_turns: false` and
`voice_turns: false`; source inspection is not a capability declaration for a
live service.

All routes require a device principal. Server-side grants bind that principal
to canonical profile IDs and allowed operations. The server checks ownership of
every profile, conversation, turn, request, and audio object. A client-supplied
ID never establishes ownership. A revoked/expired credential returns `401` on
capabilities, submission, request/turn polling, cancellation, speech retry and
audio fetch; the worker also rechecks authorization before execution and before
releasing audio. Never use the broad MCP bridge credential as a Tamagochi
runtime credential, even if Godot hides tools locally. The model tool dispatcher
must enforce the authenticated principal's grants on every call.

| Proposed route | Request | Success and failure behavior |
| --- | --- | --- |
| `GET /pet/v1/capabilities` | None beyond auth | `200`; contract version, permitted profiles, `text_turns`/`voice_turns` flags, accepted formats and limits. Keep both flags false until implemented. |
| `POST /pet/v1/conversations` | `{ "profile_id": "limo", "conversation_id": "optional-existing-id" }` | Without `conversation_id`, create and return `201`. With a valid owned ID, resume and return `200`. If a supplied ID is absent or belongs elsewhere, return `404`; never silently create a replacement. |
| `POST /pet/v1/conversations/{conversation_id}/turns` | `{ "request_id": "client-uuid", "text": "...", "surface": "pet_text" }` | New accepted turn returns `202` with `turn_id`, `request_id`, `stage`, `revision`. Same request ID and identical payload returns the original turn (`200`). Same ID with changed payload or a second active submission in the same conversation returns `409`. UTF-8 text limit: 16 KiB. Voice upload over 1 MiB returns `413`; unsupported MIME/container returns `415`; malformed/oversized decoded audio returns `422`. |
| `GET /pet/v1/requests/{request_id}` | Original request ID | `200` with the associated turn; `404` means definitely absent at this service; `503` with `outcome: "unknown"` means submission/execution outcome cannot be resolved. Never issue a new request automatically after `unknown`. |
| `GET /pet/v1/turns/{turn_id}` | Turn ID | `200` with authorized turn state; `404` if absent or not owned. Every state change increments `revision`. |
| `POST /pet/v1/turns/{turn_id}/cancel` | Empty object | `200` with `cancel_requested`, `cancelled`, or `completed`; cancellation cannot roll back committed tool effects. |
| `POST /pet/v1/turns/{turn_id}/speech-retry` | `{ "request_id": "client-uuid" }` | `202` when synthesis is queued from that turn's stored reply; identical retry ID returns the same job (`200`); changed payload conflicts (`409`); no completed reply returns `409`. Must not invoke the companion runtime or its tools again. |
| `GET /pet/v1/turns/{turn_id}/audio` | Turn ID | `200` with `audio/wav` only when authorized audio exists; `404` if unavailable/not owned. Do not accept arbitrary audio URLs from the client. |

A conversation has at most one active turn. Serialize submissions and return
`409` while one is active. Store `(principal_id, request_id)` durably with a
payload hash before dispatch; exact repeats resolve to that record, changed
payloads conflict. This deduplicates acceptance, but does **not** guarantee an
exactly-once model/provider invocation or external tool side effect across a
crash. Record `unknown` when execution may have started but cannot be proven;
require reconciliation or user action before another tool-capable turn. Check
the credential again before starting each queued stage and before any tool
dispatch. Credential revocation must prevent later execution, polling, and
artifact access.

Proposed turn JSON:

```json
{"profile_id":"limo","conversation_id":"owned-id","request_id":"uuid","text":"...","surface":"pet_text"}
{"turn_id":"server-id","request_id":"uuid","stage":"accepted","revision":1,"transcript":null,"reply_text":null,"speech_status":"not_requested","error":null}
```

The `stage` enum is `accepted`, `transcribing`, `responding`, `synthesizing`,
`cancel_requested`, `completed`, `failed`, `cancelled`, or `unknown`.
Nonterminal stages: `accepted`, `transcribing`, `responding`, `synthesizing`,
`cancel_requested`. Terminal stages: `completed`, `failed`, `cancelled`,
`unknown`. `revision` is monotonic and clients ignore older revisions.
Cancellation races are explicit: `cancelled` means work stopped before a result
was committed; `completed` means completion won the race; `cancel_requested`
means stop is pending. None of these undo a tool action already committed.

Keep transcript history needed for conversation resumption separate from
canonical Companions memory. Existing profile memories and closeouts remain the
only durable companion memory. Do not claim cross-surface transcript sharing.
On a submission timeout, query `/requests/{request_id}` before considering
recovery: `404` is a definitive absence at this service and permits the same
request ID to be retried; `503 unknown` is ambiguous and forbids a blind new
request. Even the same ID cannot promise exactly-once downstream execution.

## Explicit voice extension (same proposed turn routes)

`POST /pet/v1/conversations/{id}/turns` has two proposed content types:
`application/json` with `text` for text turns, or `multipart/form-data` with a
JSON `metadata` field (`kind: "voice"`, stable `request_id`,
`surface: "pet_voice"`, optional `language_hint`) and one `audio` WAV file.
Use mono, 16 kHz, signed PCM16; the 30-second sample ceiling is 960,000 PCM
bytes. Cap the complete WAV upload at 1 MiB including bounded header/metadata;
cap recognized transcript text at 16 KiB UTF-8 as well. The server validates
MIME/container, decodes the WAV, and enforces duration
from decoded sample count; it must not trust a declared header duration. Enforce
limits on the client and server. A transcription failure ends in `failed`
without invoking the companion. The normal sequence is `accepted` →
`transcribing` → `responding` → `synthesizing` → `completed`.

For voice turns, retain the real transcript and reply text. If TTS fails after a
reply exists, the turn is still `completed` with `speech_status: "failed"` and a
typed speech error; captions remain available. `speech-retry` sets
`speech_status: "pending"` then `ready` or `failed`, while the turn stays
`completed` and the revision advances. It synthesizes only the saved reply.
Audio is returned through the authorized audio route, with bounded ephemeral
retention as a requirement, not an existing guarantee. Inspect adapter/provider
logs and temporary files before claiming retention or deletion is enforced.
Diagnostics include IDs and stage timings, not audio or content by default.

The `pet_voice` surface hint requests 1–3 short sentences by default, expandable
when asked; it does not replace identity prompts or truncate replies blindly.
Start with a 10-second acknowledgement deadline, 120-second overall client
deadline, and one-second polling with backoff. These are prototype defaults,
not measured latency promises. A client deadline does not cancel server work.
Switching creatures stops local capture/playback and increments a selection
generation immediately; callbacks must match profile, conversation, turn and
generation before presentation. Attempt cancellation and reconcile old work;
never automatically play an old completed reply on returning to a creature.

Possible self-hosted candidates for evaluation are [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
(multilingual base/small models, not `.en`) for STT and
[Piper voice catalog](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md)
with its [HTTP API](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/API_HTTP.md)
for TTS. These are shortlist candidates, not selected or installed. There are
no per-call vendor fees for self-hosting, though compute, storage and operating
costs remain. Runtime benchmarks, English/Spanish/code-switch quality, voice
stability across languages, exact model/voice licenses and retention behavior
still need evaluation. Distinct character mappings appear feasible; one stable
bilingual voice for each character is not established.

## Server-side safety and implementation sequence

1. Add a dedicated Tamagochi principal and credential provisioning path with
   explicit expiry/revocation and least-privilege grants for only Limo/Hilo.
   Existing data-model support is present; a client-safe provision/revoke flow
   is missing. Prefer signed Ed25519 requests or short-lived bearer credentials
   stored in the OS credential store. Never ship provider/backend bridge
   secrets in the app. Keep auth enabled in deployment and verify its actual
   settings; source defaults alone are not deployment evidence.
2. Before any model execution, enforce profile-scoped operations at the
   backend tool-dispatch boundary for the authenticated Tamagochi principal.
   Do not use the MCP bridge credential or rely on client-side hiding or prompt
   filtering. For an initial conversation-only policy, grant only minimum
   hydration/context reads and deny `remember`, closeout, file/record writes,
   messaging, prompt edits and administrative approvals. No generic read-only
   tool setting currently provides this. Until a supported approval UI/flow is
   wired, deny approval-gated actions server-side; never infer consent from
   generated or transcribed speech.
3. Identify or implement the companion conversation runtime boundary; none is
   present in this checkout. Then add text turn persistence, ownership checks,
   request deduplication, authorized status reads, cancellation semantics and
   audit-safe errors. Do not claim asynchronous behavior until restart/recovery
   and failure semantics are specified.
4. Add STT and TTS behind the same adapter, input/output size caps, bounded
   ephemeral audio handling, and speech-only retry. Measure shortlisted engines
   with English, Spanish and code-switched speech before choosing them.

## Verification scope and worker receipt

Source review covered `profile_os/api.py`, `access.py`, `storage.py`,
`mcp_server.py`, `bridge.py`, `harness.py`, `openai_smoke.py`, `adapters.py`,
`request_limits.py`, `ACCESS_CONTROL.md`, and the repository routing/testing
instructions. No tests were needed for this documentation-only change. The
reviewer separately reports 53 passing existing access/auth/TOTP tests
(`tests/test_access.py`, `tests/test_auth.py`, `tests/test_totp_approvals.py`);
those validate existing controls, not proposed Tamagochi endpoints. No live
service, provider, audio path, or external runtime was exercised.

Worker receipt: Luna high; one bounded task plus one bounded review revision;
token and quota usage unknown. Hilo reviewed the document against source and
ran the existing access/auth/TOTP tests listed above.

## Deployment questions still open

This is a source inspection, not a live deployment audit. Verify which service
is deployed, the actual auth/read-only flags, credential/grant setup, MCP and
backend bridge scope, persistent OAuth signing-key configuration, logging and
audio retention paths, and whether any external runtime exists outside this
checkout. Also decide transcript retention/expiry and whether conversations
created by Tamagochi are intentionally isolated from other chat surfaces.
