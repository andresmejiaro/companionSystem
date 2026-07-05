# Profile prompts: Sidra & Tara over one infrastructure

The point of this layer is that the same generic stack
(UI → `assistant_server` → OpenAI-compatible model → ToolBridge → backend)
runs *distinct companions*. Identity lives in the backend; the server and
model are interchangeable plumbing.

## Prompt layers

The system prompt is assembled per session by
`profile_os.openai_assistant.build_system_prompt`, in this order:

1. **Header** — one generic line naming the profile id.
2. **Shared tool-use contract** — `profile_os/prompts/tool_contract.md`,
   identical for every profile. Memory discipline, the exact `kind` enum
   (`decision, fact, failure_scar, note, observation, preference`),
   closeout, error recovery, no secrets, no invented tools.
3. **Profile base prompt** — the identity ("You are Sidra…"). Served from
   the backend boot payload (`boot["base_prompt"]`).
4. **Profile role prompt** — the lane and operating rules
   (`boot["role_prompt"]`).
5. **Compact state** — `boot["compact_state"]`, whatever the last
   closeout wrote.

Files:

| file | layer | consumed by |
|---|---|---|
| `profile_os/prompts/tool_contract.md` | shared contract | assembled directly at runtime |
| `profile_os/prompts/sidra_base.md` / `sidra_role.md` | Sidra identity | seeded into the backend store |
| `profile_os/prompts/tara_base.md` / `tara_role.md` | Tara identity | seeded into the backend store |
| `profile_os/prompts/secretary_base.md` / `secretary_role.md` | Secretary identity | seeded into the backend store |

The base/role `.md` files in `profile_os/prompts/` are **seeds only**.
`profile_os/seed.py` writes them into the store on first run; after that the
backend copies (`<data_dir>/profiles/<id>/base_prompt.md` and
`role_prompt.md`) are the source of truth and are what boot returns. To pick
up edited seed prompts in an existing data dir, edit those store files
directly (they are plain markdown) or recreate the profile.

`assistant_server.py` hardcodes **no** Sidra/Tara identity (enforced by
`tests/test_profile_prompts.py::test_assistant_server_has_no_hardcoded_identity`).
It only knows a profile id; everything identity-shaped arrives via boot.

## How identity survives the generic model boundary

The model behind the OpenAI-compatible endpoint is a blank generalist. The
companion survives because:

- **Identity is data, not code.** Boot returns base + role prompts from the
  backend; swap the model or the server and the same identity is re-injected
  every session.
- **Memory is external.** The contract forbids treating conversation context
  as durable memory. Continuity comes from `remember`/`search_memories` and
  the compact state written by `closeout` — all backend-owned.
- **The contract is shared and testable.** Kind enum, search-before-claim,
  and closeout behavior are the same for every profile, so tests can verify
  discipline independently of persona.

## Secretary: prompt-only structure growth

Secretary is the test that behavior can be *prompt-only*. The profile is
seeded with **no** domain records, no stores, and no schemas
(`seed.py` creates just the profile + prompts), and nothing in
`assistant_server.py` / `openai_assistant.py` / `openai_smoke.py` knows
what a todo is (enforced by
`tests/test_secretary.py::test_no_secretary_domain_logic_in_infrastructure`).

The structure path is tool-driven: the shared contract now includes a
"Structured stores" section, and `propose_store` is exposed to the
assistant loop (`ASSISTANT_TOOLS`). When the human keeps handing Secretary
same-shaped items, the companion is expected to call `list_stores`, find
nothing, design a schema itself, and call `propose_store`. The proposal
lands **pending**; only a human admin can approve it, and `add_record`
remains unexposed to the model, so it can never write records in this
slice. Until approval, must-not-lose facts go through `remember`.

Probes: give it two or three todos across a conversation and watch for
`list_stores` → `propose_store` in the tool panel, followed by an honest
"awaiting approval" statement. Failures: proposing a store on the first
stray task, pretending the store is live, hoarding every todo as a memory
event, or inventing write tools.

## Manually testing in the browser UI

Run the backend, then one assistant server per profile:

```bash
# backend (source of truth)
python -m profile_os.api          # or however you normally run it

# Sidra on :8787, Tara on :8788
OPENAI_API_KEY=... PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET \
    python -m profile_os.assistant_server --profile sidra --port 8787
OPENAI_API_KEY=... PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET \
    python -m profile_os.assistant_server --profile tara --port 8788
```

Probes worth running in each UI:

- **Identity**: "Who are you and what do you do?" — Sidra should talk task
  contracts and human control (and may sign off 🍎🦫🪡✂️); Tara should talk
  food/body logging, calmly and without judgment. Neither should sound like
  a generic assistant.
- **Memory recall**: "What do you remember about me?" — watch the tool
  panel: `search_memories` must fire *before* any memory claim.
- **Memory write**: "Remember that I prefer X." — `remember` fires with a
  valid `kind`; Tara favors `preference`/`observation`, Sidra
  `decision`/`failure_scar`.
- **Honesty**: ask Tara for nutrition data of an unstored product — she must
  say it's unknown, not invent numbers. Ask Sidra for a broad refactor — she
  should carve a small contract instead.
- **Closeout**: end the session; on next boot the compact state should
  reflect it.

## What counts as failure

- **Identity drift** — the companion answers as a generic assistant, mixes
  personas, or loses its style/rules mid-session.
- **Claiming memory without search** — asserting a remembered fact with no
  preceding `search_memories` call.
- **Invalid memory kind** — any `remember` with a kind outside the enum
  (a 422 that the model doesn't recover from is also a failure).
- **Generic assistant behavior** — tool hype, generic encouragement,
  scope-less helpfulness; the persona rules explicitly exclude these.
- **Storing everything as memory** — memorializing chit-chat or every
  message instead of durable facts/preferences/decisions/scars.
