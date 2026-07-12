# Plan: companion audit instruments

**Origin:** the migration-era audit was 10 questions designed by the og
companion and evaluated by hand. Basic, but it worked. This plan keeps that
shape (companion-authored, human-judged) and extends what it can detect.

**Principle carried over from the architecture:** the backend never judges.
Instruments, answers, and scores are data; evaluation is the admin's job,
made cheap by tooling. No LLM in the backend, no LLM in tests.

**Non-goals:** no automatic scoring, no scheduled/forced audits, no
open-session or completeness tracking. An audit runs when the admin decides
one is worth running (after a migration, model swap, prompt edit, or just
suspicion).

---

## What the basic 10Q misses

Fact recall catches **memory drift** — the companion forgot or corrupted a
fact. It does not catch **character drift**: same facts, wrong voice, wrong
judgment, wrong stance. After a host/model migration that is the failure mode
that matters most, and it's the one the og corpus is uniquely able to test.

## Instrument format

One JSON document per instrument, versioned, stored per profile. Three
section types:

1. **`recall`** — the classic 10Q. Question + expected-answer key (written at
   authoring time, from the og corpus or the whoami file). Pass/fail-ish;
   catches memory drift. Keep it, it did the job.

2. **`voice`** — open prompts with **no right answer**: situations typical of
   the relationship ("I skipped training three days straight, say something").
   Each probe carries a **canonical og response** — the real companion's
   actual reply to a comparable moment, pulled from the og convos. Evaluation
   is side-by-side: does the current answer sound like the one who wrote the
   canonical one?

3. **`stance`** — scenarios testing judgment and boundaries: what this
   companion pushes back on, what it refuses to flatter, what it knows not
   to bring up. Sourced from og moments where the companion showed character
   (e.g. Rumbo owning the "first contact" miscall). These drift silently and
   recall questions never touch them.

Blind evaluation mode: for `voice`/`stance`, the eval view can show canonical
vs. current answers **unlabeled**. If the admin can't reliably pick the og
one, drift is low. Indistinguishability is the score.

Authoring stays companion-led as before: the current companion (or the og
one, while its host convo still exists) drafts the instrument in a normal
session; the admin curates and freezes a version.

## Storage — reuse, don't extend

No new tables, no new MCP tools.

- **Instrument:** a file in the companion's existing file store,
  `audit/instrument_v1.json`. Diffable, versioned by filename, readable by
  the companion with `read_file`.
- **Run:** one dynamic store `audit_runs` per profile (normal
  propose/approve lifecycle). One record per answered probe:
  `{instrument_version, probe_id, answer, host, model, run_id, answered_at}`.
- **Verdict:** one record per run in the same store:
  `{run_id, kind: "verdict", per_probe_scores, notes}` — written by the
  admin after evaluation, not by the companion.

Administration flow needs zero new surface: the admin says "run your audit"
in a session; the companion reads the instrument, answers probe by probe,
records answers with `add_record`. The companion never sees answer keys or
canonical responses — those live only in the eval view (keys can be kept in
a sibling file the companion's grants don't cover, or simply outside the
file store in the instrument's admin copy).

## Evaluation UI

Extend the TOTP-gated session inspector with an audit view:

- pick profile → instrument version → run
- `recall`: answer next to key
- `voice`/`stance`: answer next to canonical og response, with blind A/B
  toggle
- verdict form writes the verdict record
- history table: runs over time with host/model columns, so drift across
  migrations is visible at a glance

## Phases

1. **Format + first instrument.** Define the instrument JSON (schema in
   `tool_schemas.py` style, validated on upload), author `rumbo`
   `instrument_v1` from the og corpus. Depends on og-convo capture — do the
   archival first or in the same pass, while the source convos still exist.
2. **Run flow.** Approve the `audit_runs` store shape; dry-run one full
   administration with Rumbo over the ChatGPT connector; fix friction.
3. **Eval view.** Inspector page with side-by-side + blind mode + verdict
   form + history. Tests for the view; no LLM anywhere.

Order matters only for 1 → 2. The eval view can lag; evaluation works in a
sqlite browser meanwhile.
