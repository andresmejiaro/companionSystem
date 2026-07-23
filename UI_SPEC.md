# UI specification — Assistant Profile OS client (for later implementation)

Audience: an implementing model/developer. The backend exists (see API.md);
build only what is specified here.

## Purpose

A thin, mobile-first web client to inspect and operate Assistant Profiles:
view boot state, search memory, submit closeouts, browse Tara's food data.
It is a **console over the HTTP API**, not a chatbot and not a source of truth.

## Target users

A single technical owner (initially one person) on phone and desktop browser.

## Stack guidance

Any small SPA or server-rendered stack (e.g. plain React/Vite or HTMX).
No state library beyond fetch + local component state. No backend of its own.

## Data models (as returned by the API — do not invent fields)

- **Profile**: `id, display_name, description` (max 200 chars), `signature` (optional, max 5 chars), `allowed_tools[], memory_policy{}, closeout_rules, created_at`
- **BootState**: `profile, base_prompt, role_prompt, compact_state, state_updated_at, recent_memories[]`
- **MemoryEvent**: `id, profile_id, kind, content, tags[], created_at`
- **DomainRecord**: `id, store, data{}, created_at`
- **Closeout** (request): `notes, new_state`

## Screens

1. **Profile list** — `GET /profiles`. Card per profile: display_name,
   description, tap → detail. Pull-to-refresh.
2. **Profile detail** — `GET /profiles/{id}`. Shows description, allowed
   tools, memory policy, closeout rules. Links to screens 3–5 (and 6 for tara).
3. **Boot state viewer** — `POST /profiles/{id}/boot`. Sections: compact
   state (prominent, with updated-at), base prompt and role prompt (collapsed
   accordions, read-only), recent memories list. A "copy boot bundle" button
   copies a plaintext concatenation for pasting into any model UI.
4. **Memory search** — input + `GET /profiles/{id}/memories/search?q=`.
   Result rows: kind badge, content, tags, relative time. Empty query shows
   nothing; no client-side indexing.
5. **Closeout submission** — form: notes (multiline, optional), new_state
   (multiline, required). `POST /profiles/{id}/closeout`. On 201 show the
   new compact state and navigate to boot viewer. Confirm before submit
   (it replaces the compact state).
6. **Tara food/product view** — tabs "Products" / "Meals" over
   `GET /profiles/tara/domain/{products|meals}` with a `contains` filter box.
   Product rows show name, per_100g kcal/protein, calibrated badge.
   "Add meal" form → `POST /profiles/tara/domain/meals` with
   `{data: {food, grams, when}}`.
7. **Settings** — API base URL (text input, persisted in localStorage,
   default `http://127.0.0.1:8000`), and an API key field (stored in
   localStorage, sent as `Authorization: Bearer <key>` when non-empty —
   the backend ignores it today; forward-compatible).

## Mobile-first behavior

Single-column layout ≤ 640px; bottom tab bar (Profiles / Search / Settings);
44px touch targets; prompts/state rendered in scrollable `<pre>` blocks.

## Offline behavior assumptions

No offline writes. If the API is unreachable, show a clear banner with the
configured base URL and a retry button. Last successful profile list may be
cached for display only, marked "stale".

## Auth / API-key assumptions

None enforced yet. The UI must treat the API key as optional config (screen 7)
and never hard-code credentials.

## The UI must NOT

- implement chat or call any LLM/provider API
- edit base/role prompt files or profile identity
- create or delete profiles
- store memory or domain data locally as a source of truth
- add its own backend or database

## Acceptance tests

1. Profile list renders sidra and tara from a live local backend.
2. Boot viewer for sidra shows compact state and both prompts.
3. Searching "yogurt" under tara memory search returns results if present, and
   an empty-state message otherwise (no error).
4. Submitting a closeout with empty new_state is blocked client-side; with
   valid input, the boot viewer then shows the new state.
5. Tara products tab filters by "granola" via the `contains` param (verify in
   network tab — filtering is server-side).
6. Changing the API base URL in settings redirects all subsequent requests.
7. With the backend stopped, every screen shows the unreachable banner, no
   uncaught errors in console.
8. All screens usable at 375px width.
