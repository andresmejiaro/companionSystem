# Architecture note — slice zero

## Reuse/build decision (PROVISIONAL)

Web verification was not performed in this session; the evaluation below is
from training knowledge (as of early 2026) and is explicitly **provisional**.
Re-verify before slice two if reuse becomes attractive.

| Project | Known character | Fit vs. target | Verdict |
|---|---|---|---|
| **mem0 / OpenMemory** | Memory layer for LLM apps; extraction/dedup pipelines; OpenMemory adds local MCP server | Memory only — no profile registry, prompts, compact boot state, closeout, or domain stores. Extraction pipelines typically assume an LLM (often an API key) in the loop, which violates "no LLM in tests". | ~40% |
| **Letta (ex-MemGPT)** | Full agent server: agents with persistent memory blocks, tools, model-agnostic | Closest conceptually (agent ≈ profile, core memory ≈ compact state). But it is an *agent runtime* — it wants to own model execution and the agent loop, which this system explicitly defers. Large surface for one person to inspect/modify. | ~55–60% |
| **Graphiti / Zep** | Temporal knowledge-graph memory | Graph DB dependency (Neo4j etc.), LLM-driven ingestion, no profile/prompt/closeout concepts. Explicitly excluded by "avoid graph databases in slice zero". | ~30% |
| **projectmem** | Not confidently known; cannot describe without fabricating | Unverified | n/a |
| **Generic MCP memory servers** (e.g. reference memory server) | Simple KV/graph memory over MCP | No profiles, prompts, state lifecycle, or domain stores. | ~25% |

None reaches the 80% bar. The gap is consistent: existing projects solve
**memory**, while this system's core is the **profile lifecycle**
(registry → prompts → boot → remember/search → closeout → compact state)
plus per-profile **domain databases**. That lifecycle is ~300 lines of
boring Python; adapting a large framework around it would cost more than
building it. **Decision: build a small custom slice.** mem0/Letta remain
candidates as *optional memory backends* behind the same interface later.

## Design

```
profile_os/
  storage.py   Store — all persistence + core ops (boot/remember/search/closeout/domain)
  api.py       FastAPI HTTP layer, thin wrapper over Store
  seed.py      example profiles: sidra, tara
  adapters.py  FakeModelAdapter (deterministic; real providers later)
  errors.py    typed errors → 404/422
```

- **SQLite** (`data/profile_os.db`): profiles, memory_events, compact_state,
  domain_records, closeouts. One file, inspectable with any sqlite client.
- **Markdown prompt files** on disk per profile: base_prompt.md, role_prompt.md.
  Deliberately *outside* the DB so identity files are visible, diffable, and
  never touched by memory operations.
- **closeouts.jsonl** per profile: append-only human-auditable session log.
- **Search** is case-insensitive substring over content+tags. Boring on purpose;
  swap in SQLite FTS5 or embeddings later behind the same `search()` signature.
- **Closeout compaction boundary:** the backend never summarizes. Producing
  the new compact state is the caller's job (the model running the session);
  `closeout()` validates `new_state` is non-empty and stores it verbatim,
  logging `notes` for audit only. This keeps the backend LLM-free.
- **SQLite connections** are one-per-thread (`threading.local` in `Store`),
  each with `PRAGMA foreign_keys = ON`. FastAPI's threadpool can therefore
  call the same `Store` from any worker thread safely, with no shared
  connection and no ORM.
- **Domain stores (legacy)** are generic `(profile_id, store, json)` records
  from slice zero, kept for compatibility. New structured data should use
  dynamic stores.

## Dynamic stores (slice two)

The platform does **not** hardcode Tara/Sidra/Rita/Rumbo domain schemas.
Predefined schemas would make the platform decide, ahead of time, what every
assistant may store — wrong for a system meant to host arbitrary profiles.
Instead:

1. **Profile proposes** a store (`name`, `purpose`, `proposed_by`, schema).
2. **User/admin approves or rejects** it (audited).
3. **Backend enforces** the schema on every record write.

Dynamic stores are *not* memory events: memory events are free-text session
memory; dynamic stores are durable structured data validated against an
approved schema.

Implementation: `profile_os/dynstores.py`, a separate `DynamicStores` service
sharing the same SQLite database (tables `dynamic_stores`, `dynamic_records`,
`store_audit`) — kept out of `Store` to avoid a god object. Schema format is
a tiny custom subset (see module docstring / API.md) rather than full JSON
Schema: ~60 lines of explicit validation, zero dependencies, and it can be
swapped for JSON Schema behind `validate_record()` later.

Versioning: schemas are immutable; re-proposing an archived/rejected name
increments the version and restarts the approval cycle. Records carry the
`schema_version` they were validated against. **Schema migrations are
intentionally out of scope** for this slice.

⚠️ Approval is a workflow, not security: until per-profile API keys and an
admin key exist (see security plan below), any caller can hit the approve/
reject/archive endpoints. Auth middleware will be inserted at the FastAPI
layer, mapping keys → allowed profile_ids and marking lifecycle endpoints
admin-only.

## HTTP-first, MCP later

Slice zero ships **HTTP only** (smallest inspectable option): it's testable
with plain httpx, serves the future web/mobile UI directly, and OpenAPI docs
come free. The MCP adapter plan: a thin MCP server exposing tools
`boot`, `remember`, `search`, `closeout`, `query_domain`, `add_domain_record` —
each a 1:1 call into the same `Store` methods (or the HTTP API when remote).
No logic will live in the MCP layer.

## Provider agnosticism

The backend stores configuration and state; it never calls a model.
`adapters.FakeModelAdapter.run(system_prompt, user_message)` defines the
execution interface future Claude/GPT/Gemini/local adapters implement.
Model choice is runtime config, not part of any profile's identity.

## Config: local vs cloud

Local config is env-var based (`PROFILE_OS_DATA_DIR`). Cloud deployment later
= same Docker image + a mounted volume (or swap SQLite→Postgres behind Store).
No cloud assumptions exist in the code.

## Security plan (documented, not implemented)

- **Later:** per-profile scoped API keys, checked by HTTP middleware; key →
  allowed profile_ids + operations. Stored hashed in a `credentials` table.
- No shared god token for normal use; an explicit admin key is required for
  cross-profile writes and profile creation/deletion.
- Secrets must never be stored in memory events (later: a regex/entropy
  screen on `remember`).
- Prompt/identity files are not writable through any memory or closeout
  endpoint — already true structurally in slice zero (no endpoint writes them).
