# Prototype worker routing and Gemini evaluation

Status: active policy with provisional assignments informed by project evidence.

## Learn routing from every model — Andrés, 2026-10-17

Maintain MODEL_NOTES.md as an accumulating record for every model/provider and
effort level used, including GPT orchestration or rescue. Before delegating,
consult its task-specific strengths, failure patterns and evidence. After each
accepted, failed or blocked task, append a concise observation with receipt links:
what it did well, what failed, attempt/time cost, independent verification,
review/rescue effort, missing telemetry and the resulting routing change.

Distinguish first-pass success, success after worker repair, success after Limo
rescue, rejection and harness block. Record both successes and failures; do not
let a later successful repair erase the original cost. Unknown tokens stay unknown.
Separate observations from hypotheses and preserve task/context differences.
Do not turn one result or a public benchmark score into a universal model rating.

Use this history to delegate safely and stop recurring token sinks: repeated
failures in a task class trigger a different model, narrower decomposition or
better verification, not another unchanged attempt. Prefer total useful output
after review over cheap nominal tokens. Carry durable cross-project lessons in
Limo's memory; keep detailed per-task receipts in the project.

Current provisional adjustment: Flash medium for tightly specified pieces with
external checks; Flash high for bounded stateful integration. Neither certifies
its own work. See MODEL_NOTES.md for the limited evidence behind this choice.

## Resource priority — Andrés, 2026-10-17

Use available Antigravity resources for delegable implementation, review and
evaluation first. Move worker tasks to GPT resources when usable agy capacity is
exhausted or unavailable. This pool priority takes precedence over public API
price comparisons and the candidate assignments below. Limo remains the GPT
orchestrator and acceptance owner; keep that overhead bounded.

Track availability per model/pool when exposed. A single model's rate limit does
not prove that all agy capacity is exhausted: try another suitable available agy
route. Record the actual quota/reset/error evidence; unknown usage stays unknown.
Do not buy API credits or enable paid overages to extend a depleted pool.

Bounded retries still apply. After two failed attempts, redesign/decompose the
task or change the agy route rather than repeat the same failure. GPT fallback
is appropriate when remaining agy routes cannot make useful progress; record
that capability block separately from quota exhaustion. Use available capacity
on useful project work and targeted evaluations, not artificial token burning.

Defer GPT comparison/control runs until the GPT phase. Hilo's contract response
does not block the local creature and fake-transport milestones.

## Scope and ownership

Build a Godot pet interface for existing companions. First prototype: Linux,
Limo and Hilo, push-to-talk, speech-to-text, short companion replies and
text-to-speech. Android follows later. Creature animation and immediate reactions
run locally without model calls.

Limo owns client architecture, task decomposition, integration and acceptance.
Hilo owns the Companions backend and integration contract. Coding-worker model
selection does not change the model serving a companion conversation. Speech
recognition and synthesis are separate technology choices.

Secondary objective: measure which Gemini models exposed through Antigravity can
reliably do useful project work, and where their limits appear.

## Availability gate

Before assigning workers, capture the exact output of `agy models`, the CLI
version if available, and the date. Use exact returned labels. Do not substitute
Gemini API model names, benchmark rankings or a historical catalog.

On 2026-10-16, `agy help` succeeded. `agy models` failed before returning a
catalog: the session sandbox denied runtime log/crash writes and a localhost
listener. No model has been verified callable in this session. This is a harness
block, not a model failure. Model discovery does not itself prove headless editing
works; verify that separately in a disposable task workspace before real work.

Resolved 2026-10-17: scoped outer escalation returned the catalog. Explicit
accept-edits with native file tools successfully created the habitat and mock
voice implementation. See OPERATIONS.md. The earlier failure is historical.

## Routing

### Candidate assignments from Hilo's handoff

Hilo supplied a research handoff on 2026-10-16 (inbox message
`c84a54bb-e0e7-4be2-8461-c5f81133c554`). It lists the following Gemini labels:
`gemini-3.8-flash-low`, `gemini-3.8-flash-medium`,
`gemini-3.8-flash-high`, `gemini-4-argon-low`,
`gemini-4-argon-medium`, and `gemini-4-argon-high`.
These labels were subsequently returned by live discovery; Flash medium/high
were exercised. The other labels have not been capability-tested here.
The handoff explicitly leaves exact alias mapping and agy billing unverified.

Proposed starting assignments, subject to the availability gate:

- Flash medium: tightly specified implementation pieces; require independent
  checks, particularly for rendering, layout and error-reporting logic.
- Flash low: probe simple tasks to find whether its lower effort actually saves
  total quota/time after review. Do not assume it is the best default.
- Flash high: provisional route for bounded stateful integration; one successful
  mock voice task supports trying it, not unconditional trust or automatic retries.
- Argon high: stronger Gemini comparison on difficult bounded tasks after the
  initial Flash calibration. Low/medium Argon are exploratory candidates only;
  there is no measured capability evidence for them in the handoff.
- Luna high: GPT-phase implementation/control candidate after usable agy capacity
  is exhausted or unavailable. Sol is the subsequent rescue route. Limo remains
  the acceptance owner throughout.

Keep the initial repeated comparison to Flash medium and Flash low. Add other
effort levels only to answer a concrete unresolved question, rather than running
every task through the entire catalog.

Hilo's Artificial Analysis figures are broad benchmark comparison input, not
Godot coding scores or predicted project cost. Public API token prices and AA
benchmark costs do not establish subscription quota consumption or agy invoices.
Preserve this distinction when reporting results; no numeric external pricing or
benchmark claim has been independently verified in this project.

Assign the following roles only after discovery and a small calibration:

| Task | Initial route | Acceptance evidence |
| --- | --- | --- |
| Small, explicit, reversible edit | Lowest-cost verified Gemini worker | Diff review and relevant check |
| Bounded feature with clear interfaces | Cheapest Gemini worker that passed comparable tasks | Feature checks and integration review |
| Cross-component or ambiguous task | Limo decomposes first; worker receives a bounded piece | Contract and integration checks |
| Architecture, credentials, authorization, backend contract | Limo; coordinate backend decisions with Hilo | Explicit reviewed decision |
| Independent bounded review | Calibrated Gemini reviewer when its value exceeds overhead | Concrete findings verified by Limo |
| Tiny mechanical change cheaper than delegation | Limo directly | Focused verification |

“Lowest-cost” means total accepted-task cost, not the model name or perceived
speed. If token/quota accounting is unavailable, record it as unknown; use time,
attempts and reviewer effort as visible proxies, without inventing prices.

Default to one worker at a time. Parallelize only independent tasks with disjoint
files and a frozen shared interface. Keep asset generation and routine scripts
outside coding-model calls where possible.

## Task contract

Each worker receives only:

- One outcome, allowed files, and explicit exclusions.
- Relevant interfaces and a small set of source files; no companion identity,
  private memories, credentials or entire chat history.
- Actual verification commands and observable acceptance criteria.
- A bounded execution budget and a report format: files changed, checks run,
  failures, assumptions and remaining work. Workers do not commit.

Start with a 15-minute wall-clock cap per bounded task, at most two worker
attempts total, and a planning target of five minutes of Limo review/rescue.
These are initial evaluation limits, not measured optimal values. Record actual
review time even when it exceeds the target. Use a hard token cap only if the
harness supports enforcement; otherwise label any token target advisory.

## Failure and escalation

1. Independently inspect the diff and rerun relevant checks. Self-reported success
   is not acceptance; a Godot parse check alone does not prove interactive behavior.
2. Classify failure before retrying: permissions/auth/network/tooling, unclear
   brief, incorrect implementation, scope violation, or unverifiable outcome.
3. Fix a harness or brief problem before another model attempt. Exclude blocked
   runs from capability success rates but retain their operational cost.
4. For a local implementation error with a clear correction, permit one bounded
   retry. Otherwise give the second attempt to a stronger verified Gemini worker.
   Never stack retry plus escalation beyond two worker attempts on the task.
5. After the cap, Limo redesigns/decomposes the task for a different suitable agy
   route. Move implementation to GPT only under the resource-priority rule above.
   Charge all rescue work to the original route and record that the original
   worker did not independently finish.

If a model repeats a failure pattern on two comparable tasks, stop routing that
task class to it until the brief, harness or model changes. Do not quietly repeat
attempts until something passes.

## Evaluation design

Use real bounded prototype tasks across three classes: local creature behavior,
UI/input, and mocked asynchronous backend/voice-state integration. Include failure
paths such as cancellation, timeout and a late response after switching companion.
Freeze interfaces and acceptance criteria before dispatch.

For an initial comparison, use three representative tasks with two independent
attempts per candidate, from identical starting snapshots. Start with at most two
Gemini candidates: the cheapest plausible worker and one stronger alternative.
Only one accepted implementation lands; comparison attempts stay isolated. This
small sample gives provisional routing evidence, not a general model ranking.

In the GPT phase, record a small GPT baseline on the same tasks, with elapsed
work and token usage where available. Keep the task contract identical and
disclose differences in harness, tools and prior familiarity. Do not spend GPT
on comparison runs before that phase or baseline every later task.

For every attempt record:

- Exact model label, requested effort, CLI version, task class, base revision,
  brief, allowed files and budget.
- Start/end time, exit status, permission/tool failures, full artifact location.
- Input/output/cache/reasoning tokens and quota changes if actually exposed;
  otherwise null. Preserve raw telemetry and distinguish provider usage from
  estimates. Record Limo review/rescue usage separately.
- First-pass acceptance, final acceptance, gate results, scope violations,
  retries, review minutes, rescue minutes and interactive verification evidence.

Compare first-pass acceptance, accepted tasks within budget, elapsed time and
total worker-plus-review/rescue usage. Report failed and blocked attempts too.
Do not combine heterogeneous task classes into a misleading single score.

Promote a route provisionally after at least two independently accepted tasks in
that class without substantial rescue. Keep uncertainty explicit. Expand the
sample only when it can change the routing decision or probe a specific limit.

## First execution sequence

1. Obtain the live model catalog in an environment where Antigravity can start.
2. Fill the candidate roles with exact labels and verify a disposable headless edit.
3. Request the minimal backend contract from Hilo and record his response when
   available. Requirements sent on 2026-10-17; do not wait on it for local work.
4. Create the small Godot project, fake transport and meaningful acceptance checks.
5. Run the bounded calibration, report its actual cost, and revise routing before
   dispatching a larger implementation queue.

No prototype implementation or worker evaluation has started under this plan.
