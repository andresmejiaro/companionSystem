# Model observations

The client observations below were copied from
`/home/andres/tamagochi/MODEL_NOTES.md` on 2026-10-17 at Andrés's request.
Their receipts are relative to that repository; they are Limo's evidence,
not independently rerun backend checks. Backend observations are appended below.

An accumulating task-specific record, not a leaderboard. Append evidence for all
models and effort levels we use. Link raw receipts rather than pasting full logs.
Consult before dispatch and update after review. Keep successes, failures and
rescue costs visible together. Routing remains provisional until comparable
tasks establish a pattern.

## 2026-10-17 — gemini-3.8-flash-medium / agy 1.4.0

Task: Godot 4.6.1 Linux habitat, two procedural creatures, local behavior and UI.

- Did well: produced the project structure, distinct gecko/fox drawings, shared
  behavior component, wandering/petting and usable integration methods. Native
  file tools obeyed accept-edits without broad permission bypass.
- Failed: self-intersecting tail polygon; offscreen layout; controller A consumed
  before focused UI; tests reported success after a script error. The last issue
  makes independent error-sensitive verification essential.
- Repair: one worker revision fixed most issues but still needed Limo changes
  for headless minimize behavior, aspect sizing and import error checking.
- Cost: initial edit run about 4.5 minutes; worker repair about 3.5 minutes;
  roughly 3 minutes direct Limo rescue, plus review/checking. Exact tokens/quota
  unknown. Permission-blocked and hung output-only runs were additional harness
  costs, not coding capability failures.
- Outcome: accepted with orchestrator rescue; not first-pass success.
- Routing hypothesis: useful for tightly specified pieces, costly to supervise
  when UI/rendering and test harness correctness are bundled together. Do not
  use its self-reported gate result as evidence of correctness.
- Evidence: evals/habitat-initial/review.json,
  evals/habitat-accepted/review.json, evals/habitat-edit/result.json,
  evals/habitat-repair/result.json and evals/session/habitat-repair-check.log.

## 2026-10-17 — gemini-3.8-flash-high / agy 1.4.0

Task: simulated asynchronous voice loop on the existing accepted habitat.

- Did well: separate fake transport/coordinator/audio fixture, monotonic revision
  checks, selection generations, cancellation, stale-reply rejection, retained
  captions on synthesis failure and repeatable failure/retry tests.
- Observed failures: none in the bounded acceptance checks; no implementation
  repair required. This does not establish production readiness.
- Cost: about 6 minutes worker time; focused Limo source review, gate and rendered
  smoke. Exact tokens/quota and review minutes unknown.
- Outcome: first implementation passed import and 115 total project assertions;
  real rendered AudioStreamPlayer lifecycle checked with dummy audio.
- Limits: fixed replies/chirps, no real capture/STT/TTS/backend, no physical
  controller/Deck or audible hardware checks. Existing scaffolding and a more
  precise brief helped; this was not the same task as medium's habitat.
- Routing hypothesis: provisional candidate for bounded stateful integration,
  with Limo retaining acceptance. No causal claim about effort-level superiority.
- Evidence: evals/mock-voice-high/result.json,
  evals/mock-voice-high/review.json, evals/session/mock-voice-check.log and
  evals/session/visual-smoke.log.

## 2026-10-17 — GPT-6.1 Sol low / Limo

Task: planning, orchestration, permissions diagnosis, review and bounded rescue.

- Did well: rejected false-green tests, inspected actual rendering, retained
  receipts and repaired remaining habitat issues; enforced resource reserve.
- Failed: stopped at planning despite sufficient execution authorization;
  initially missed agy's accept-edits option and used an avoidable output-only
  detour. User steering exposed both process failures.
- Cost: exact tokens unknown. Shared five-hour meter went from reported 57% to
  18%, then 10%, while Hilo also worked; cannot attribute that drop to this run.
- Routing change: act on existing authorization; check native edit-only mode
  before detouring; keep orchestration context narrow and explain worker status.

## Next observations

Argon, Flash low and GPT worker models have no direct task evidence in this
project yet. Hilo's Luna work is a separately owned receipt; do not count it as
our own independently verified coding run. Add model entries when exercised.

Entry fields: date; exact model/provider/effort; task and starting context;
did well; failed; verification and outcome; attempts/elapsed/review/rescue;
observed token/quota telemetry or unknown; confidence; routing change; receipts.

## 2026-10-17 — gpt-6-luna high / Hilo backend workers

Two distinct tasks, started under the now-superseded session override:

- Runtime hardening: first pass produced useful bounded async HTTP transport,
  history, reasoning replay and limits, but missed message role/status validation
  and boot checks on scoped context reads. Accepted after one worker correction,
  with 18 focused tests independently rerun. Reported worker time approximately
  12 + 5 minutes; initial 15-minute budget plus an eight-minute correction cap.
- App wiring: disjoint files and frozen runtime interface, accepted first review;
  worker reported 21 focused checks passed. Exact elapsed time unknown.
- Independent combined regression: 131 passed in 10.43 seconds. No provider
  calls, production work, or commits. No orchestrator implementation rescue;
  review corrections were sent back to the worker. Exact review minutes and
  worker/orchestrator token/quota telemetry unknown.
- Observation: Luna produced usable backend code with targeted review; first-pass
  passing tests did not establish complete authorization/response validation.
  This does not compare Luna causally with Gemini on different Godot tasks.
- Routing: future tasks use Andrés's newly requested Antigravity-first policy,
  consult the imported class-specific evidence, and reserve Luna for documented
  GPT fallback. Require adversarial validation for authentication/runtime work.
- Receipt: [backend acceptance](ops/tamagochi/2026-10-17-runtime-review.md).

## 2026-10-17 — Hilo requirement correction / no API keys

- Andrés clarified runtime conversations must use Codex or agy account access.
  The key-based Responses implementation does not fulfill the runtime delivery
  requirement despite synthetic correctness. Removed its environment activation
  with a small direct edit under the mechanical-change exception.
- Regression: 131 passed in 10.50s, including ordinary-app failure to enable a
  runtime from legacy key settings. Setup and handoff corrected; CLI adapter
  implementation remains pending. No deployment or model call.
- Discovery only: agy 1.4.0 models failed inside sandbox on logs/listener, then
  succeeded with scoped escalation; codex-cli 0.163.0 exec help inspected.
  No agy coding worker, quota/model-capability claim, or fallback was involved.
- Lesson: confirm account-execution versus API billing requirements before
  implementing a provider-specific adapter. Passing tests cannot establish
  agreement with an unstated runtime requirement. Telemetry/review time unknown.

## 2026-10-17 — Hilo orchestration / GPT runtime

- Reviewed backend architecture and diffs, rejected incomplete first-pass
  validation, required one bounded correction, independently reran tests and
  updated setup/handoff/routing documentation.
- Current parent model label/effort and exact token/quota/review-time telemetry
  were not exposed by this execution harness; do not infer them from Limo's
  Sol entry or historical companion runtime preferences.
- Routing lesson: a function named `list` can mutate shared state; inspect
  helpers used by read-only dispatch. Synthetic success is local acceptance,
  not proof of provider credentials, model quality, voice, or deployment.
- Receipt: [backend acceptance](ops/tamagochi/2026-10-17-runtime-review.md).
