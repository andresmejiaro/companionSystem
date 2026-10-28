# Repository instructions for coding agents

## Tamagochi delegation and resource constraints

For Tamagochi work, follow [TAMAGOCHI_ROUTING.md](TAMAGOCHI_ROUTING.md),
copied from `/home/andres/tamagochi/ROUTING.md` at Andrés's request.
Hilo applies the same delegation, resource priority, worker budgets, bounded
retries, verification, and reporting constraints as Limo. Hilo is the backend
orchestrator and acceptance owner here; Limo retains client ownership.

Use suitable available Antigravity capacity first, then GPT only under the
documented fallback conditions. Do not buy credits or enable paid overages.
Keep worker context narrow and exclude companion identities, private memories,
and credentials. Tiny mechanical changes may be done directly as the routing
policy allows.

[TAMAGOCHI_SESSION_PLAN.md](TAMAGOCHI_SESSION_PLAN.md) is a verbatim copy of
Limo's session plan for reference. Apply its resource and orchestration
discipline; its client deliverables, reported quota, model selection, and
one-hour execution clock describe Limo's run, not a newly started backend run.
Explicit session instructions from Andrés take precedence.

Policy update (2026-10-17): Andrés superseded the session-only Luna override
and requested Limo's updated Tamagochi routing for all future workers. Use
Antigravity first, with GPT only under the documented fallback conditions.
Consult [MODEL_NOTES.md](MODEL_NOTES.md) before delegating and append verified
outcomes after every accepted, failed, or blocked task. Flash medium is a
provisional choice for tightly specified pieces; Flash high for bounded
stateful integration. These assignments require live availability verification
and independent acceptance, not model self-certification.

Follow [TAMAGOCHI_OPERATIONS.md](TAMAGOCHI_OPERATIONS.md) for the verified agy
accept-edits/native-file workflow and before/after quota snapshots. Prefer
delegating implementation; Hilo retains backend architecture, review, and
acceptance. Preserve the two-attempt limit, narrow worker context, and no
paid overages. The superseded Luna override is historical only.

Runtime requirement (2026-10-17): Tamagochi companion AI interactions must use
existing Codex or Antigravity account-authenticated execution. Do not request,
provision, or activate a provider API key. The isolated Responses prototype is
not the runtime delivery path. Keep text/voice capabilities disabled until a
CLI adapter has been implemented and independently accepted; coding-worker
routing and the model serving a companion remain separate decisions.

## Testing: read this before diagnosing failures

Read [`TESTING.md`](TESTING.md) completely before running, changing, or
diagnosing the test infrastructure.

In particular:

- FastAPI's standard `TestClient` is verified working in this repository's
  Python 3.14 environment.
- `ThreadedASGIClient` in the MCP tests is a compatibility name currently
  aliased to `TestClient`; it is not a custom per-request thread wrapper.
- Do not attribute a stall to Python 3.14, Starlette, HTTPX, AnyIO, or
  `TestClient` until the exact diagnostic in `TESTING.md` has been run.
- Messages such as `Failed to create stream fd: Operation not permitted`
  indicate the execution sandbox may be interfering. Re-run the documented
  diagnostic or test command with the appropriate sandbox escalation before
  changing application or test-client code.
- Run tests as `.venv/bin/python -m pytest`, not bare `pytest`, so imports and
  dependency versions come from the repository environment.

Do not replace `TestClient` with a custom event-loop/thread wrapper unless a
real incompatibility remains after following the fallback procedure in
`TESTING.md`.

