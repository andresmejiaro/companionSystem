# Backend runtime acceptance receipt — 2026-10-17

Base revision: `bb44c73a04c44d3c893f37bb9ce979ef5313440c`, with existing dirty
work preserved. No commit created. These are local synthetic-provider results.

## Worker 1: runtime hardening

- Model: `gpt-6-luna`, effort high, native collaboration worker. Narrow context;
  no companion identities, memories or credentials supplied.
- Allowed files: `profile_os/pet_runtime.py`, `tests/test_pet_runtime.py`.
- Initial budget: 15 minutes; worker reported approximately 12 minutes.
- First attempt: 12 focused tests passed, but review required stricter message
  role/status validation, boot checks before scoped context reads, deadline
  checks around local tools, and stronger adversarial cases. Not first-pass
  acceptance. Review also identified the store-list helper's implicit write;
  worker removed that path during implementation.
- Second/final attempt: eight-minute budget, approximately five minutes reported;
  18 focused tests passed and were independently rerun (18 passed in 1.58s).
- Outcome: accepted after worker correction. No orchestrator implementation
  rescue; orchestrator updated the module's status docstring after acceptance.
- Limits: network deadline is cancellable, synchronous local reads are checked
  between operations. Real provider behavior and cross-turn reasoning replay
  are not verified or claimed.

## Worker 2: app integration

- Model: `gpt-6-luna`, effort high, native collaboration worker.
- Allowed files: surgical edits to `profile_os/api.py`,
  `profile_os/pet_conversations.py`, new
  `tests/test_pet_runtime_integration.py`.
- Budget: 15 minutes. Exact elapsed time not exposed in the final report.
- Ran independently of the correction pass with disjoint files and frozen
  runtime `run`/`supports`/`close`/factory interface.
- Worker focused check: 21 passed. Source review confirmed opt-in factory wiring,
  worker draining and cleanup in finally blocks. No repair requested.
- Outcome: accepted on first review with independent combined regression below.

## Independent acceptance

Before implementation, existing regression: 108 passed in 8.93s.

Final command:

```sh
.venv/bin/python -m pytest tests/test_pet_runtime.py tests/test_pet_runtime_integration.py tests/test_pet_auth.py tests/test_pet_conversations.py tests/test_api.py tests/test_auth.py tests/test_access.py tests/test_totp_approvals.py -q
```

Result: **131 passed in 10.43s**. Inspected runtime, tests, app wiring and service
shutdown source. Provider simulated using `httpx.MockTransport`; no network or
billable model call. Covers role history, request deduplication, per-profile
configuration, unknown timeout without replay, revocation, mutation refusal,
boot/scoped grants, read-only hydration, aggregate limits, drip-fed deadline,
and runtime shutdown. Existing lifecycle tests cover cancellation and restart.

Exact worker/orchestrator tokens, cached/reasoning tokens, quota deltas and
orchestrator review minutes were not recorded; unknown, not zero. Worker times
above are worker reports, not independently timed telemetry. Worker conversation
artifacts are `/root/runtime_hardening` and `/root/runtime_wiring` in this session;
they were not exported as raw local transcripts.

## Routing update

Both workers began under Andrés's Luna session override. After completion,
Andrés explicitly superseded it with Limo's updated Antigravity-first policy.
Future delegation reads `MODEL_NOTES.md`, verifies live agy catalog and editing
mode, captures pool quota before/after, and uses bounded attempts. No agy run
was necessary for these already-completed tasks, and none is claimed.
