# Agent execution notes

## Antigravity permissions — 2026-10-17

Two separate boundaries caused the first run failure:

1. Codex's sandbox blocked agy's runtime writes and localhost listener. A scoped
   escalation for `agy models` succeeded and returned the live catalog.
2. agy's headless worker requested a shell command, could not prompt for its own
   permission, and exited before writing app files. This was a harness failure,
   not evidence of Flash coding inability or exhausted quota.

Use explicit `--mode=accept-edits --sandbox` for implementation. Native file
operations are approved; shell commands retain their separate permission rules.
Tell workers to use native file tools, stay within the brief's allowed files,
and leave shell commands and checks to Limo. Do not use the broad permission
bypass flag. Execution mode is not an OS-enforced file-scope restriction, and
existing command allow rules still apply.

The installed agy-delegate relay supports plan mode via `--read-only`, but does
not currently expose accept-edits. Use a workspace-local extension or explicit
agy invocation; do not silently edit the global skill. Verify the mode with an
actual bounded edit before claiming headless writes work.

Verified in this session: Flash medium created app files under explicit
accept-edits with no permission bypass. Workspace-local scripts/agy-relay.mjs
adds --accept-edits to the installed relay. First implementation failed Limo's
independent acceptance (rendering and false-green test defects); this is separate
from the now-working edit permission path.

Reference: https://www.antigravity.google/docs/cli/modes/

Keep agy resources first, GPT worker resources afterward. Announce each worker's
model, task and boundaries, then distinguish worker execution from Limo review
and repair. Shared GPT usage includes Hilo; keep orchestration focused.

## Quota snapshots

Verified: `agy --print=/usage --output-format json` returns remaining quota and
reset times without a model turn. `/quota` is its alias. Save before/after each
worker; compare numeric remaining_fraction fields per pool, allowing for resets
and concurrent use. This is pool usage, not an exact task token invoice. The
usage object's zero tokens belong to the read-only query, not prior workers.
First after-session snapshot: evals/session/agy-usage-after.json. No starting
snapshot was taken, so this session's exact quota delta cannot be reconstructed.
