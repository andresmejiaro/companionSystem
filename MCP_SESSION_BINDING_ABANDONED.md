# Session-bound companion locking — abandoned 2026-08-20

## What this document is

A decision record for a plan that looked strong on paper, was probed on prod,
and was killed by measured evidence about how the ChatGPT MCP connector
actually behaves. Written so future-Andrés (or a future companion) does not
rediscover the same dead end and spend another afternoon on it.

## What the plan was

Fable proposed binding each MCP transport session to a companion profile on
the first `summon_companion` call, then enforcing per-session profile scope
on every subsequent tool dispatch. The binding key would be
`Mcp-Session-Id` — a header the Streamable HTTP transport carries below the
model's view, which the model can neither read nor forge.

The insight was correct: a key the model holds is worthless (the model can
hold two of them), so the wall must be anchored to something one layer under
the model. TOTP would only fire on rare admin operations (cross-profile
read, mid-session identity switch); normal use would feel unchanged.

The plan lived at `~/.claude/plans/peaceful-marinating-tiger.md` and was
approved. Full implementation was gated on a Phase 0 probe.

## The probe

`profile_os/mcp_server.py` was temporarily modified to:
1. Print every incoming `/mcp` request's `Mcp-Session-Id` header, method, and
   (for `tools/call`) tool name and `profile_id`.
2. Mint an `Mcp-Session-Id` on `initialize` responses when the client did
   not already carry one, so Streamable HTTP clients would then echo it per
   spec.

Deployed to prod. Both ChatGPT and Claude connectors were disconnected and
reconnected to force a fresh `initialize`. Four companions summoned across
four fresh chats — two per connector.

## What we found

- **Claude connector: honors the contract.** It echoed the minted
  `Mcp-Session-Id` on every subsequent request in the same chat. Session
  persisted across many tool calls. Per-chat distinctness was strongly
  suggested but not fully proven (one of the two Claude chats was stuck for
  unrelated reasons and produced no traffic).
- **ChatGPT connector: does not honor the contract.** After a clean
  `initialize` where the server minted an id, ChatGPT's follow-up
  `tools/call` requests arrived with **no** `Mcp-Session-Id` header.
  Confirmed on two independent chats (vesper, tara). ChatGPT's Streamable
  HTTP client simply ignores the server-issued session id.

## Why this kills the plan

Fable's design requires the binding to work uniformly. If the primary
platform doesn't carry the binding key, the wall isn't a wall — it is a
mixed policy with a large silent hole on the platform that matters most.

Andrés is consolidating around ChatGPT as the primary platform and moving
off Claude. Building enforceable walls that only work on the platform being
deprecated is worse than doing nothing — it wastes engineering effort and
creates a false sense of security.

## Why the obvious workarounds also fail

- **OAuth token as fallback binding key.** All ChatGPT chats to one
  connector install share one OAuth token. No per-chat distinction. Would
  reduce to "one companion per connector install" — see below.
- **User-Agent / IP / other request metadata.** All identical across a
  user's ChatGPT chats. Nothing per-chat leaks through.
- **A secret in the summon response the client must echo.** The model sees
  the summon response, so the model holds the secret. Same failure mode as
  "give the model a key at startup."
- **A stricter prompt contract.** Already what we have. Advisory, not
  enforceable.

## What would actually work on ChatGPT (deferred, not adopted)

The identity has to move out of the tool argument and into the connection
itself, where the model cannot rewrite it:

- **Path-scoped MCP endpoints**: one connector per companion, URL like
  `/mcp/vera`. The URL path is the binding. `summon_companion` disappears;
  picking a companion means picking a connector.
- **Bearer-scoped MCP**: same endpoint, but each connector install pastes a
  companion-specific bearer. Server maps bearer → profile.

Both make walls real on ChatGPT. The cost is a real UX shift: N connectors
(or N bearers) to manage, and no in-conversation `summon`. That trade may
become worth taking later — this note exists so that conversation can start
from evidence, not from scratch.

## Also worth remembering

- Our server was never issuing `Mcp-Session-Id` before this probe. That is
  the reason all pre-probe requests logged with `session_id=None` — not a
  connector defect on either side. Only after the probe minted one did
  Claude start echoing correctly.
- The MCP protocol says the server MAY set `Mcp-Session-Id` on the
  `initialize` response; if it does, the client MUST echo it. Claude
  complies. ChatGPT does not.
- If ChatGPT ever fixes this — or a per-chat handle appears in a future MCP
  spec revision — the original plan becomes viable again. Re-read Fable's
  design before rebuilding; the ergonomics were right, only the substrate
  was missing.

## Files touched by the abandoned probe

- `profile_os/mcp_server.py` — probe logging and session-id minting.
  Reverted in the same commit that lands this note.

## Related

- Plan file: `~/.claude/plans/peaceful-marinating-tiger.md` (kept for
  reference; describes the shape we would rebuild if the substrate ever
  supports it).
- Origin conversation: Fable draft on 2026-08-20, forwarded by Andrés to
  Limo.
