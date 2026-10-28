# Backend session handoff — 2026-10-17

## Deployed after Andrés's explicit instruction

Release `tamagochi-20261017-disabled-1` is deployed on the existing backend.
Text/voice false, `PET_RUNTIME_ENABLED=0`, no API key or CLI conversation
runtime activated. Isolated container regression: 170 passed in 56.52s;
live authentication/capabilities/revocation, existing API and connected MCP
checks passed. MCP and Life containers unchanged. Online SQLite backup and
rollback image retained. Deployment-only smoke credential revoked. See
`ops/tamagochi/2026-10-17-deployment.md` for exact restart/rollback commands;
the remote git tree was deliberately not changed. Earlier “not deployed”
statements below describe history and are superseded here.

## Latest correction: account CLI execution, no provider API key

Andrés clarified that AI interactions must use Codex or agy, not an API key.
The Responses prototype remains available only through explicit code injection
for synthetic tests; its environment activation has been removed. The ordinary
app's factory returns None even with legacy PET key/model settings. Both
capability flags remain false. Implement and accept an account-authenticated
CLI adapter before real companion conversations; prior API-key activation
instructions are superseded. No real conversation runtime is delivered yet.

Read Limo's mail `d9e69d66-8bc0-4cef-9a1c-2e43de8fb05d`: recommends deploying
only the reviewed backend slice with runtime disabled, preserving unrelated
dirty work, checking existing API/MCP plus pet authentication and false flags,
with rollback. Deployment has not been performed in this session.

CLI discovery: codex 0.163.0, agy 1.4.0; agy models required scoped outer
escalation for runtime logs/listener and returned the Gemini labels in routing.
This was a harness permission issue, not quota exhaustion. No worker or CLI
model conversation was launched. Future implementation follows agy-first
routing, narrow worker context and actual quota snapshots.

## Resumed session: accepted local text adapter

Supersedes the isolated/unaccepted provider status below. The Responses adapter
is wired through `runtime_from_environment` only with explicit configuration;
the default remains disabled. Reviewed role-tagged history, completed-response
validation, intra-turn encrypted reasoning replay, current boot/tool grants,
read-only hydration, aggregate limits, cancellable network deadline, and
request-scoped HTTP cleanup. App shutdown drains workers and closes runtime;
startup/shutdown cleanup releases the executor lock.

Independent final regression: 131 passed in 10.43 seconds using the command in
`TAMAGOCHI_BACKEND_SETUP.md`. Includes HTTP integration with a simulated
Responses provider: sequential turns/history, duplicate request IDs, unknown
timeout without replay, unconfigured profile rejection, revocation before
tool dispatch/publication, and shutdown. No real provider calls, new real
device credentials, deployment, purchases or commits. Voice remains false;
STT/TTS, audio endpoints and retention expiry remain unimplemented. Synchronous
local reads cannot be forcibly timed out; checks stop subsequent work.

Workers used `gpt-6-luna` high under the then-current session override. Runtime
hardening needed one correction pass; wiring passed first review. Token/quota
telemetry was unavailable. See `MODEL_NOTES.md` and
`ops/tamagochi/2026-10-17-runtime-review.md` for outcomes and evidence.

Andrés subsequently replaced the Luna override with Limo's updated policy.
`AGENTS.md` now routes future workers to Antigravity first, consults model notes,
uses bounded retries and quota snapshots, and retains GPT only as documented
fallback. `TAMAGOCHI_ROUTING.md` and `TAMAGOCHI_OPERATIONS.md` are fresh copies
from `/home/andres/tamagochi`; imported client evidence in `MODEL_NOTES.md` is
attributed to Limo, not backend acceptance.

Next: verify the explicitly selected model/provider in an authorized runtime
environment, then implement/evaluate speech adapters. Do not infer real model
quality or voice readiness from the synthetic tests. Preserve unrelated dirty
work in this checkout.

## Earlier session history (superseded provider status)

Stopped new implementation when Andrés reported 18% of the rate window
remaining, below the copied session policy's 20% reserve. Luna handled the
implementation tasks; Hilo reviewed and ran regression checks. Worker token
and quota consumption were not exposed.

## Reviewed slice

- Dedicated registered, expiring and revocable device bearer credentials;
  local issue/revoke CLI; pet authentication enforced independently of the
  general API development flag. Transactional provisioning and scoped grants.
- Durable text conversation/turn lifecycle: ownership, explicit resume,
  stable request-ID deduplication, serialized active turns, revisions,
  polling, cancellation, and interrupted/uncertain outcome handling.
- Runtime-enabled execution requires a single process holding the store lock.
  Disabled runtime leaves the general API process topology unchanged.
- Setup and commands: `TAMAGOCHI_BACKEND_SETUP.md`.

The ordinary application has **no accepted model runtime**. Capabilities remain
`text_turns: false`, `voice_turns: false`; new text submissions return 503.
Lifecycle execution was verified with injected synthetic runtimes only.
No deployment, real credentials issued, provider calls, or purchases occurred.

## Isolated work to resume

`profile_os/pet_runtime.py` and `tests/test_pet_runtime.py` are an unaccepted
provider prototype, unwired from the application. Its passing mock tests are
not provider acceptance. Before enabling it, complete these review items:

- Hard total execution deadline, bounded aggregate tools and context.
- Proper user/assistant history and completed provider response validation.
- Recheck boot authorization before each context read; verify read-only
  dispatch and revocation with adversarial integration tests.
- Complete encrypted reasoning replay and HTTP client shutdown handling.
- Explicit per-profile configuration and supported launch/demo instructions.

Then review and test the provider slice; do not replay an uncertain turn or
claim cross-surface transcripts. Speech adapters, audio retention, speech-only
retry and voice endpoints remain future work. No validated STT/TTS engine is
configured. Local transcripts currently have no automatic retention expiry.

## Verification

Hilo's regression command covers pet authentication/lifecycle and existing API,
auth, access and TOTP tests: 108 passed. Luna separately ran those two pet test
files plus the isolated runtime mock tests: 21 passed. `git diff --check` passed.
Existing unrelated dirty work was preserved; nothing was committed.
