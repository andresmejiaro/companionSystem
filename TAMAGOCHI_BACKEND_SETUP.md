# Tamagochi backend device bootstrap (prototype)

This checkout exposes authenticated text conversation and turn lifecycle routes
alongside `GET /pet/v1/capabilities`. Voice remains unavailable. Text turns are
advertised only when an in-process runtime is explicitly configured; the default
app has no runtime and rejects new submissions with `503 runtime_unavailable`.
Device credentials, conversations, and turns share the Companions SQLite
database and access-control tables. Provisioning is local CLI only; there is no
HTTP credential-provisioning route and no OAuth flow.

Andrés requires Codex or Antigravity account-authenticated execution for AI
interactions, with no provider API key. The Responses adapter is an isolated
synthetic-test prototype, not the approved deployment runtime.
`runtime_from_environment` returns no runtime, including when legacy
`PET_RUNTIME_ENABLED`, model, or API-key variables are present. Text and voice
capabilities therefore remain false in the ordinary app. Local transcripts have
no automatic expiry.

## Launch

Choose the same existing data directory used by the Companions service. Keep
general API authentication enabled in the service environment:

```sh
PROFILE_OS_DATA_DIR=/srv/companions/data PROFILE_OS_AUTH_ENABLED=1 \
  .venv/bin/uvicorn profile_os.api:app --host 127.0.0.1 --port 8000
```

Do not configure `PET_OPENAI_API_KEY` or enable the historical Responses
prototype. Keep `PET_RUNTIME_ENABLED` unset or disabled for deployment.

The pending Codex/agy CLI adapter must preserve current grant checks and bounded
hydration, provide process cancellation/deadlines and bounded output, and run
without inherited broad MCP tools or uncontrolled host access. A CLI read-only
mode alone does not prove profile isolation. Verify the exact account/model
route, quota behavior, transcript/log retention and service-user access before
advertising text capability. Do not copy account tokens into source or the
client. CLI runtime model choice is distinct from coding-worker routing.

Codex noninteractive execution supports saved CLI authentication and explicit
sandbox settings according to the [official OpenAI documentation](https://developers.openai.com/codex/noninteractive).
Installed CLI discovery found Codex 0.163.0 and agy 1.4.0. This is discovery,
not acceptance of a companion conversation runtime. No CLI model conversation
was invoked during the requirement correction.

The pet routes independently require a registered device bearer even if
`PROFILE_OS_AUTH_ENABLED=0`; the setting above also protects the rest of the
Companions API. Use the actual service's existing data directory when issuing
credentials.

## Issue and use a device credential

Issue a one-hour credential for the selected existing canonical profile(s).
The CLI requires `--data-dir` explicitly and accepts an expiry up to 24 hours:

```sh
.venv/bin/python -m profile_os.pet_devices_cli --data-dir /srv/companions/data \
  issue --profiles hilo,limo --expires-hours 1 --device-name "living-room-pet"
```

The command prints the bearer once. Copy it directly into the running client
session; keep it in memory for that session, or use an operating-system
credential store if the client supports one. Do not put it in preferences,
source control, shell history, logs, or support reports. The optional
`--identity-read` flag adds the profile-scoped identity read grant.

Check capabilities by entering the bearer at a no-echo prompt. This keeps it out
of command arguments and shell history:

```python
from getpass import getpass
from urllib.request import Request, urlopen

token = getpass("Device bearer: ")
request = Request("http://127.0.0.1:8000/pet/v1/capabilities",
                  headers={"Authorization": f"Bearer {token}"})
with urlopen(request) as response:
    print(response.read().decode())
del token
```

Capabilities contain only profiles granted to that device, contract version,
limits, and runtime availability flags; they never return a token. A configured
runtime supports:

- `POST /pet/v1/conversations` with `profile_id`; pass `conversation_id` only
  to resume an existing conversation owned by that exact device credential.
- `POST /pet/v1/conversations/{conversation_id}/turns` with `request_id`, UTF-8
  `text` up to 16 KiB, and `surface: "pet_text"`.
- `GET /pet/v1/requests/{request_id}` and `GET /pet/v1/turns/{turn_id}` for
  authorized state; `POST /pet/v1/turns/{turn_id}/cancel` requests cancellation.

An identical request ID and payload returns the original durable turn. A
changed payload conflicts. An ambiguous provider result is recorded as
`unknown`, returned by request polling with HTTP 503, and blocks further turns
in that conversation. There is no automatic replay. Transcript rows are
separate from Companions memory and are not shared with other chat surfaces.

When a pet runtime is configured, one API process must own pet-turn execution
for its data directory. Startup takes an exclusive lock and marks leftover
nonterminal turns `unknown`; a second runtime-enabled process using the same
data directory refuses startup. With no runtime configured, ordinary API
process topology is unchanged and no pet recovery or executor lock runs.
Runtime adapters must bound provider requests and use the checkpoint callback
before costly work and side effects. No provider is configured by default.

The initial grants are `pet:converse`, `boot`, `search`, and `records:read` for
each selected profile.

## Revoke

Use the credential ID printed alongside the bearer:

```sh
.venv/bin/python -m profile_os.pet_devices_cli --data-dir /srv/companions/data \
  revoke <credential-id>
```

The service checks registration, credential expiry/revocation, principal
disablement, and profile grants on each pet request. Revocation therefore
invalidates subsequent requests without restarting the API. The prototype
does not yet provide OAuth enrollment, secure cross-device token delivery, a
validated real-provider deployment, voice turns, speech retry, or audio fetch.

## Local verification

All fixtures use synthetic profiles, temporary databases and a simulated HTTP
provider, with no billable model calls:

```sh
.venv/bin/python -m pytest tests/test_pet_runtime.py tests/test_pet_runtime_integration.py tests/test_pet_auth.py tests/test_pet_conversations.py tests/test_api.py tests/test_auth.py tests/test_access.py tests/test_totp_approvals.py -q
```

Earlier 2026-10-17 adapter acceptance: 131 passed in 10.43 seconds.
The later no-API-key correction also verifies that legacy key settings cannot
enable the real application runtime. This verifies local adapter,
HTTP lifecycle, authorization, history, deduplication, cancellation/revocation,
unknown outcomes, limits and deadline behavior; it does not verify voice or a
real model conversation.
