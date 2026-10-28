# One-hour prototype session

Proposed execution plan. No implementation run started by writing this file.

## Boundaries

- Wall clock: 60 minutes from the explicit start of execution. Worker time,
  setup, review and handoff all count. Do not start another worker after minute 45.
- GPT: 6.1 Sol low for orchestration and acceptance. Andrés reports 57% remaining
  in the five-hour rate window. This is a ceiling, not a spend target, and cannot
  be converted reliably into a fixed token count or minutes of work.
- Workers: Antigravity first; GPT workers only after usable agy capacity is
  exhausted or unavailable. No paid API fallback or overages.
- Scope: local Linux Godot prototype with Limo and Hilo. No production backend
  work, real STT/TTS integration, Android export or new infrastructure this hour.

## Intended outcome

A runnable habitat with selectable gecko and fox, distinct local idle/reaction
behavior, and an end-to-end simulated conversation lifecycle. If the full slice
does not fit, preserve a verified runnable local habitat and report the missing
voice simulation explicitly. No claim of real companion conversation.

Provisional shapes/eyes and simple animation are enough for this milestone;
finished sprite generation is a later task. Keep captions large and controls
usable without fine pointer targeting.

## Schedule and cut points

| Minutes | Work | Exit evidence / cut point |
| --- | --- | --- |
| 0–8 | Inspect actual tools: Godot version, agy catalog/auth, runtime permissions and safe worker edit capability. Check Hilo's reply. | Exact available tools/models recorded; establish launch/check commands. If agy is blocked, distinguish permission failure from quota and report it. |
| 8–12 | Freeze a tiny project skeleton, module interfaces, worker brief and acceptance checks. | One agreed coordinator contract; no dependency on real backend. |
| 12–27 | Worker A: habitat, shared creature component, two character definitions, idle/wander/pet and switch controls. | Runnable local scene, no network/model requests. Review and verify before proceeding. |
| 27–42 | Worker B: fake transport plus listening/thinking/speaking presentation, cancel and stale-response handling. | Deterministic delayed reply fixture; switching cannot animate/speak through wrong companion. Drop this task if A still needs substantial repair. |
| 42–52 | Integration and verification. Repair only acceptance failures. | Headless import/parse and behavioral checks; visible launch if display available. No new feature work. |
| 52–60 | Save clean artifacts, run instructions, results and worker receipts. | Exact working state, known failures, next task and observed resource use. |

Normal path is two serial worker briefs, with reuse of narrow shared context.
If one task runs slowly, reduce scope at the cut point; do not silently extend
the hour. Each initial worker run gets at most 12 minutes, leaving review time
inside its slot. One repair attempt is allowed only if it fits the existing slot.
These session limits are tighter than ROUTING.md's general limits and take
precedence for this run.

## Acceptance checks

1. Project launches on the local Linux machine; exact Godot version is documented.
2. Limo and Hilo can be selected, visibly differ, react to interaction and perform
   local idle activities. Switching preserves correct profile identification.
3. The mock turn visibly traverses listening, thinking and speaking; mock speech
   may use a packaged audio fixture. Do not imply real microphone/STT/TTS works.
4. Cancel and companion switching prevent a delayed reply from playing. A forced
   mock error returns controls to a usable state.
5. Idle runtime makes no network/model calls. Development workers are separate
   from application runtime.

Checks 1–2 form the minimum useful deliverable. Checks 3–5 complete the intended
slice, with idle network behavior also checked for the minimum habitat.
Controller input actions should be defined; Steam Deck device verification is
reported separately and only claimed if performed. Headless checks do not prove
visual quality, sound playback or controller focus.

## Quota and orchestration discipline

At start, around minute 30 and before final handoff, capture any exposed usage or
quota readings. If the session does not expose the five-hour meter, label it
unobservable rather than claiming an enforced percentage cap. Do not interrupt
Andrés repeatedly for meter readings.

Keep my context to the design contract, task brief, diff and gate results; keep
full worker transcripts in artifacts. Avoid repeated documentation searches,
whole-repository dumps, parallel speculative agents and GPT baseline experiments.
Automated checks handle routine validation; Limo inspects the resulting evidence.

If telemetry shows GPT's remaining window falls to 20% or less, stop new model
work and use the remaining session for bounded verification and handoff. This is
a proposed reserve, not a promise that a meter is available or that rate-limit
consumption maps linearly to tokens. Stop earlier on a rate limit. Do not switch
to another paid route just to fill the hour.

## Setup contingencies

- agy discovery/edit blocked: use a narrow approved execution request if supported.
  Do not broaden permissions or bypass them. If still blocked by minute 8, record
  agy as unavailable and use one bounded GPT worker under the fallback policy.
- agy quota exhausted: try another suitable agy model/pool where available, then
  GPT fallback. Do not loop quota failures or count them as capability failures.
- Godot missing: use a user-local verified installation in the workspace if
  permitted and quick; no system package changes. If setup consumes the first
  12 minutes, prioritize the minimal habitat and omit mock voice work.
- No graphical display: run headless checks, save exact launch instructions and
  state that visual acceptance remains unverified.
- Hilo has not answered: continue with fake transport. Contract review is not a
  prerequisite for local animation or simulated conversation states.
- No usable worker route: preserve the blocked-state evidence and prepared brief;
  report the blocker rather than quietly writing the entire prototype with GPT.

## Evaluation within the hour

Each real worker task supplies one data point: model/effort, timing, available
token/quota telemetry, files changed, gates, first-pass outcome and Limo repair
effort. Do not run the full repeated model comparison from ROUTING.md this hour.
Two different tasks do not establish a model ranking. Select one small future
comparison task based on an observed success or failure, rather than duplicating
the whole build now.

## Handoff

Record what actually runs, how to launch and check it, model labels used, worker
attempts and failure categories, observed usage (unknown where unavailable),
device checks performed, and the next concrete task. Preserve unaccepted diffs
separately. No deployment or paid service provisioning is included.
