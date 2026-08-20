# Session-bound companion locking — implementation plan (2026-08-20)

## Status — FINAL: token-only (commit 10e461c, live 2026-08-20)

The shipped shape is **token-only**. `summon_companion` mints a `session_token`
and returns it in its result; the model must echo it as the `session_token`
argument on **every** subsequent tool call. Enforcement (in
`profile_os/session_binding.py` + `profile_os/mcp_server.py`):

- **No transport headers are read.** `x-openai-session`, `x-conv-id`, and
  `Mcp-Session-Id` were all tried and **dropped** — each carried a surprise
  (notably claude.ai reusing one `Mcp-Session-Id` across different
  conversations, which false-blocked a second summon). The token is the one
  uniform key on every surface, with zero client config.
- **strict** (default): a guarded call with no valid token →
  `session_token_required`; switching companions mid-session → `session_locked`;
  cross-profile write → `session_bound`; cross-profile read blocked.
- **trusted** (one TOTP-gated `/session-gate` switch): no-token calls fall open
  (advisory), switching allowed, cross-reads allowed; cross-**writes** still
  blocked when a token identifies a session.
- Guarded set: writes (memory/records/files/ironsworn/proposals/closeout/
  exam_attempt) + reads; `send_message` is the exempt escape hatch.
- Tokens are HMAC-hashed at rest. State in `MCP_SESSION_BINDING_STATE_FILE`
  (tables `sessions`, `session_events`; the old `bindings`/`session_audit`
  tables are inert). `scripts/companions-mcp.sh` (the x-conv-id wrapper) is now
  **obsolete** — no wrapper is needed.

**Honest ceiling:** the token is visible to the model, so this is ceremony, not
a cryptographic wall — a determined model can copy its own token. It makes
*accidental* cross-companion access impossible and *deliberate* crossing
conspicuous (audit), on every client, with nothing to install. That trade was
chosen deliberately (low friction, uniform, no surprises).

The header-based design and its history are retained below for rationale only;
the token-only status above supersedes it.

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
3. `x-conv-id` header (stock CLI agents — see "Covering stock CLI clients")
4. None → no fingerprint (see Fallback).

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
- **Reads**: blocked cross-profile by default (strict), lifted only in
  trusted mode (see Trust switch). Cross-profile *writes* are always blocked.

### Summon / rebind rule — one companion per session (revised 2026-08-20)

A summon under an unknown fingerprint always succeeds and creates a new
binding — that is just "a new chat" (or a rotated id). A re-summon of the
*same* companion is idempotent and always allowed.

**Default is strict: one companion per session.** A summon of a *different*
profile Z under a fingerprint already bound to X is **blocked** (structured
error `session_locked`), and does *not* rebind — X stays bound, its writes
keep passing, Z's writes keep failing. The block happens after the backend
resolves Z (so aliases don't false-trigger) but Z's identity packet is never
returned to a session bound to X. To work as Z, start a new conversation (new
fingerprint), or have the human flip the trust switch.

Switching is allowed only when the session is **reverted to trusted** (see
Trust switch), in which case the old rebind-and-log behavior applies. Earlier
this doc treated mid-session switching as always-legitimate; that was reversed
— accidental identity drift is now impossible by default, and deliberate
switching is a conscious, human-gated act.

### Trust switch ("revert to trusted")

One master flag (`strict_mode`, default on), toggled at the TOTP-gated admin
page `/session-gate`. **Strict** (default): one companion per session +
cross-profile reads blocked. **Trusted**: both relaxed at once — mid-session
`summon` switching and cross-profile reads allowed, e.g. for an audit.
Cross-profile *writes* are blocked in both states. Takes effect immediately,
no redeploy.

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

## Covering stock CLI clients (Claude Code / Codex) — added 2026-08-20

The problem: Claude Code and Codex are general companion surfaces (any
companion may reach for one to write a file or a letter), so they need the
same summon + multi-companion + per-conversation binding as ChatGPT. But
neither emits a per-conversation key on the wire — Claude Code doesn't echo
`Mcp-Session-Id` and sends no conversation id (open, unanswered bug
anthropics/claude-code#41836); Codex is the same. The server cannot observe
one either: behind Caddy every request is `127.0.0.1`, TLS terminates
upstream, User-Agent is static. The key must be *injected client-side*.

The stock-client solution (no custom client, just the standard `mcp-remote`
bridge both CLIs already use). `x-conv-id` is fingerprint source #3, after the
two native headers. There are two ways to fill it, and which one applies was
settled empirically:

- **Claude Code** exports `CLAUDE_CODE_SESSION_ID` (equals the transcript UUID,
  resume-stable). If its `.mcp.json` `${VAR}` expansion resolves against the
  Claude Code process env, this is the nicest source (resume-stable):
  `--header x-conv-id:${CLAUDE_CODE_SESSION_ID}`.
- **Codex does NOT expose one.** Verified 2026-08-20 with an env-probe MCP
  server registered in Codex and triggered via `codex exec`: Codex scrubs the
  environment for stdio MCP children — they receive only
  `HOME, LANG, LOGNAME, PATH, SHELL, USER`. The session id Codex prints
  (`codex resume <uuid>`) never reaches the child. So no native id, and no
  ambient env passes through by default (`shell_environment_policy`).

Because of Codex, the **uniform** answer that works on both without depending
on any client's env behavior is a tiny wrapper that mints the id itself at
spawn — `scripts/companions-mcp.sh`: it generates a per-spawn UUID and bakes
it into `mcp-remote --header x-conv-id:<uuid>`. The client spawns it once per
session (that spawn is the per-conversation boundary), so no env var is needed.
Register it as the MCP command:

```
Codex:       codex mcp add companions -- /path/to/companions-mcp.sh
Claude Code: "companions": {"command": "/path/to/companions-mcp.sh"}
```

Granularity is per-spawn = per-active-conversation: concurrent companions run
as separate CLI processes (separate ids, isolated); sequential `/clear` in one
process shares the id and is handled by the rebind rule. The wrapper's uuid
rotates on resume (harmless — looks like a new conversation, rebinds); to make
a session resume-stable, export `COMPANIONS_CONV_ID` before launching, or on
Claude Code use `${CLAUDE_CODE_SESSION_ID}` directly.

**This is a trust/friction boundary, not a wall — by explicit decision.**
The model can read its own `CLAUDE_CODE_SESSION_ID` and, in a coding session,
hand-roll a `curl` to `/mcp` with a forged `x-conv-id`. That is accepted: it
is the deliberate, loud tier (visible in the transcript). The boundary's job
is to make *accidental* cross-companion writes impossible and *intentional*
ones require conspicuous effort — to give a determined agent pause, not to
stop it. It runs on stock Claude Code / Codex with nothing installed beyond
the `mcp-remote` bridge they already use.

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
