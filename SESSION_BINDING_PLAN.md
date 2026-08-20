# Session-bound companion locking — implementation plan (2026-08-20)

## Status

**Implemented 2026-08-20** in `profile_os/session_binding.py` (store +
enforcement logic) and wired into `profile_os/mcp_server.py`. Enforcement is
live: cross-profile writes are hard-blocked on both connectors; cross-profile
reads are blocked behind a TOTP-gated runtime switch (`/session-gate`) that an
audit can lift without a redeploy. Bindings + audit persist in a dedicated
SQLite file (`MCP_SESSION_BINDING_STATE_FILE`, default derived from
`MCP_OAUTH_STATE_FILE`). The `WIRE_PROBE` scaffolding is removed. Tests in
`tests/test_session_binding.py`. Decisions taken during build (all from a live
Q&A with Andrés): hard-block from day one; keep Claude at full parity via
minted `Mcp-Session-Id` (a return to Anthropic is on the table); guard writes +
reads + proposals + closeout/exam; header-only fingerprint (no `_meta`
fallback); no-fingerprint requests fail open but log a distinct greppable
warning so a vanished header never becomes a needle in a haystack; longevity
check skipped by choice ("come back when it breaks").

The historical plan below is retained for rationale. It supersedes and replaces
`MCP_SESSION_BINDING_ABANDONED.md` (deleted in the commit that lands this
file — see git history for the full dead-end analysis). The plan was
un-abandoned because a second, wider wire probe found the missing substrate.

## The problem being solved

A companion once wrote a memory into **another** companion's store instead
of sending mail. The prompt contract forbids this but is advisory. We need
server-side enforcement that a conversation locked to companion X cannot
write into companion Y's stores — without OTP/approval friction (Andrés
runs many small companion sessions per day, often several concurrently
under one ChatGPT account, e.g. Rumbo for a workout while drilling lt_rita
between sets). Any per-summon human step is unacceptable; any per-account
(OAuth-token-level) lock breaks concurrent sessions.

## The key discovery (probe of 2026-08-20)

With `WIRE_PROBE=1` logging full headers + JSON-RPC bodies on `/mcp`, two
concurrent ChatGPT chats (rita and vera summoned) showed:

- ChatGPT sends an **`x-openai-session`** HTTP header on every request
  (also mirrored in `params._meta["openai/session"]`). Example value:
  `v1/3EBKb12ESeOayMno5WJsRTnX7AaOoE83kzE8DHUMoipy1eorwtGx6EAuk6UNHEqWFDxRRClH5jyM`
- It was **stable within a chat** (identical across that chat's calls) and
  **distinct between the two chats**, while `x-openai-subject` (per-account)
  and the OAuth token were identical across both. So it is per-conversation.
- It is carried below the model's view: the model can neither read nor
  forge an HTTP header. This is exactly the binding key the original
  design needed.
- The Claude connector equivalent is `Mcp-Session-Id` (server-minted on
  `initialize`, echoed by Claude per MCP spec — verified in the first
  probe). ChatGPT ignores `Mcp-Session-Id`, which is why the earlier
  attempt was abandoned; `x-openai-session` replaces it there.

**Unverified caveats** (probe chats were only seconds long):
1. **Longevity/rotation.** OpenAI calls it a "session"; it may rotate on
   reconnect, long gaps, or next-day resumption of the same chat. Not yet
   tested. The design below tolerates rotation (see Rebind rule).
2. **Undocumented.** OpenAI could remove or rename it at any time. The
   enforcement must therefore fail open (see Fallback).

## Design

### Fingerprint

Per request, compute the conversation fingerprint, first match wins:
1. `x-openai-session` header (ChatGPT)
2. `Mcp-Session-Id` header (Claude and spec-compliant clients; keep the
   minting-on-initialize behavior from commit 339616e, which was reverted
   in cc999c4 — re-land it)
3. None → no fingerprint (see Fallback).

### Binding

- On `summon_companion(X)` with fingerprint F: upsert `bindings[F] = (X,
  now)`. Persist in SQLite (mcp service already uses SQLite for OAuth
  state), with `last_seen` refreshed on every request carrying F.
- On any **mutation** tool call (`remember`, `update_memory`, `forget`,
  `add_records`, `update_record`, `delete_record`, `write_file`,
  `delete_file`, `update_ironsworn_sheet`, `propose_prompt_edit`, ...)
  targeting profile Y with fingerprint F:
  - If `bindings[F]` exists and its profile ≠ Y → **block** and return a
    structured error telling the model to use `send_message` to reach Y
    instead. This is the wall.
  - If no binding exists for F (e.g. rotation happened mid-chat, or the
    mutation arrived before any summon) → allow if the call is
    self-consistent, and bind F to Y implicitly? **No** — implicit binding
    from a mutation would let intent re-bind. Instead: allow the write
    only if no *other* fingerprint from the same OAuth subject was bound
    to a different profile within the last N minutes **and** log it;
    simpler v1: allow + log ("unbound write"), tighten later with data.
- **Reads**: unrestricted in v1 (the incident was a write). Cross-profile
  reads can be gated later behind the same mechanism if wanted.

### Rebind rule (handles rotation)

A summon under an unknown fingerprint always succeeds and creates a new
binding — that is just "a new chat" (or a rotated id). A summon of
profile Z under a fingerprint already bound to X **re-binds** the
fingerprint to Z but logs loudly (see Audit). Rationale: mid-chat identity
switching is a legitimate pattern for Andrés; the wall's job is to stop
*silent one-argument cross-writes*, and after a re-bind any write to X
would now be blocked. A deliberate summon-then-write is visible in the
transcript and the audit log — detection, not prevention, is the accepted
ceiling for intent (no OTP allowed).

### Fallback (no fingerprint at all)

If a request has neither header, enforcement degrades to advisory: allow,
log `fingerprint=None`. Never hard-fail — an OpenAI change must not brick
the connector. Optionally alert (inbox message to admin) if `None` starts
appearing on ChatGPT traffic, meaning the header disappeared.

### Audit

Append-only `session_audit` table: ts, fingerprint, subject hash
(`x-openai-subject` or OAuth token hash), tool, target profile, decision
(allow / block / rebind / unbound-write). Blocks and rebinds are the
interesting rows. No payload contents.

### Expiry

Bindings older than ~24h since `last_seen` can be pruned; an expired
binding just means the next summon re-creates it. Nothing user-facing.

## Implementation steps

All in `profile_os/mcp_server.py` unless noted; the tool dispatch path is
`_handle_rpc` → session handling around line ~1312.

1. **Re-land Mcp-Session-Id minting** on `initialize` responses (see
   commit 339616e for the exact shape; it was reverted only because the
   plan was abandoned, not because it was wrong).
2. **Fingerprint extraction helper** — request headers →
   `(fingerprint, source)` per the priority above. Thread the fingerprint
   from `mcp_endpoint` into `_handle_rpc` (currently `_handle_rpc` does
   not receive the Request; pass the fingerprint string through).
3. **Bindings + audit storage** — two small SQLite tables in the mcp
   service's existing state DB; idempotent CREATE TABLE on startup.
4. **Enforcement guard** — a single function called before dispatching
   any mutation tool; the mutation-tool list lives in one constant next to
   it. Block returns a normal JSON-RPC tool error whose message
   explicitly says: "this conversation is bound to <X>; to give <Y>
   something, use send_message".
5. **Wire summon** — binding upsert + rebind logging inside the
   `summon_companion` handling.
6. **Tests** (`tests/`): bind-then-cross-write blocked; same-profile write
   allowed; unknown fingerprint summon binds; rebind logs; no-fingerprint
   request allowed+logged; expiry prune.
7. **Remove the probe** — delete the `WIRE_PROBE` block added in commit
   5102661 and remove `WIRE_PROBE=1` from `/opt/profile-os/.env` on the
   VPS, once the longevity check below is done.
8. **Deploy** per `DEPLOY.md` (git pull + compose rebuild on the VPS).

## Before or during implementation: longevity check

The probe is still deployed (`WIRE_PROBE=1` live on prod as of
2026-08-20). The two test chats (rita, vera) still exist. Re-enter them
after hours / a day, make one tool call each, then on the VPS:
`docker compose logs mcp | grep WIREPROBE` and compare
`x-openai-session` against the values recorded above (rita's starts
`v1/4xW7b0aK…`, vera's `v1/3EBKb12E…`). If the values held → the id is
conversation-stable and the Rebind rule is rarely exercised. If they
rotated → the design still works (rotation looks like a new chat), just
expect more "unbound write" log rows and keep v1's allow+log stance.

## What this does NOT do

- Does not stop a model that *deliberately* summons a second companion in
  the same chat and then writes — that re-bind is possible by design (no
  OTP allowed), but it is loud: visible in the transcript and flagged in
  the audit log. Accidents (the actual incident) become impossible;
  intent becomes detectable and reversible.
- Does not gate reads in v1.
- Does not depend on `allowed_tools` (advisory only, per server
  instructions).

## Related history

- First probe + abandonment analysis: git history of
  `MCP_SESSION_BINDING_ABANDONED.md` (deleted with this commit) and
  commits 25e6f06 / 923985f / 339616e / cc999c4.
- Wire probe that found `x-openai-session`: commit 5102661.
- Original design shape: `~/.claude/plans/peaceful-marinating-tiger.md`.
