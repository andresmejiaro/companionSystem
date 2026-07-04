# Assistant Profile OS — slice zero

Backend foundation for versioned, provider-agnostic Assistant Profiles
(configuration bundles: prompts, compact state, durable memory, domain data).
Profiles live **here**; Claude/GPT/Gemini UIs are future secondary surfaces.

No LLM is embedded or called. `profile_os/adapters.py` ships a deterministic
`FakeModelAdapter`; real provider adapters are a later slice.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn profile_os.api:app --reload
# → http://127.0.0.1:8000/docs  (OpenAPI UI)
```

First start seeds two example profiles (`sidra`, `tara`) into `./data/`.
Override the data directory with `PROFILE_OS_DATA_DIR=/path`.

Everything is inspectable on disk:

- `data/profile_os.db` — SQLite (registry, memory events, compact state, domain records, closeouts)
- `data/profiles/<id>/base_prompt.md`, `role_prompt.md` — plain markdown prompts
- `data/profiles/<id>/closeouts.jsonl` — append-only closeout log

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

Local only; no network, no API keys, no LLM calls.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — reuse/build decision, design, security plan
- [API.md](API.md) — HTTP API / tool contract (also mirrored as future MCP tools)
- [UI_SPEC.md](UI_SPEC.md) — spec for the later web/mobile client

## Docker (for the later cloud path — not deployed in this slice)

```bash
docker build -t profile-os . && docker run -p 8000:8000 -v $PWD/data:/app/data profile-os
```
