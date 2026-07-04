# API / tool contract — slice zero

Base URL: `http://127.0.0.1:8000`. All bodies JSON. Errors:
`404 {"detail": "unknown profile: 'x'"}`, `422 {"detail": "malformed ..."}`.

Future MCP tools map 1:1 to the starred operations.

## Profiles

- `GET /health` → `{"ok": true}`
- `GET /profiles` → list of profile objects
- `GET /profiles/{id}` → profile object:
  ```json
  {"id": "tara", "display_name": "Tara", "description": "...",
   "allowed_tools": ["log_food"], "memory_policy": {"kinds": ["fact"]},
   "closeout_rules": "...", "created_at": 1751640000.0}
  ```

## Core lifecycle

- ★ `POST /profiles/{id}/boot` → everything needed to start a session:
  ```json
  {"profile": {...}, "base_prompt": "...", "role_prompt": "...",
   "compact_state": "No meals logged today.", "state_updated_at": 1751640000.0,
   "recent_memories": [ {memory event}, ... ]}
  ```
- ★ `POST /profiles/{id}/memories` — remember. Body:
  `{"kind": "note|fact|decision|failure_scar|preference|observation",
    "content": "non-empty", "tags": ["optional"]}` → 201 + stored event
  (`id`, `profile_id`, `kind`, `content`, `tags`, `created_at`).
- ★ `GET /profiles/{id}/memories/search?q=<text>&limit=20` — case-insensitive
  substring match over content and tags, newest first.
- ★ `POST /profiles/{id}/closeout` — end session. Body:
  `{"notes": "free text", "new_state": "non-empty compact state"}` → 201.
  Replaces compact state and appends to `closeouts.jsonl`.
  **Compaction boundary:** the backend does not summarize. The *caller*
  (the assistant/model running the session) produces `new_state`; the backend
  only validates it is non-empty and stores it verbatim. `notes` is an audit
  trail, never an input to state.

## Domain stores

- `GET /profiles/{id}/domain` → list of store names, e.g. `["meals","products"]`
- ★ `GET /profiles/{id}/domain/{store}?contains=<text>&limit=50` → records:
  `{"id": "...", "store": "products", "data": {...}, "created_at": ...}`
- ★ `POST /profiles/{id}/domain/{store}` — body `{"data": {...non-empty...}}` → 201

## Guarantees

- All operations are profile-scoped; no cross-profile reads/writes exist.
- Memory operations can never modify prompt/identity files.
- Auth: none in slice zero (local only). Per-profile scoped API keys will be
  added as HTTP middleware later — see ARCHITECTURE.md security plan.
