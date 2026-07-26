# Companions

Companions is a self-hosted operating layer for persistent AI companions.
It keeps a companion's prompts, working state, memories, files, messages, and
structured data under your control. The service is model- and provider-neutral:
it does not call an LLM itself. Instead, an HTTP API and a remote MCP server let
Claude, ChatGPT, or another client use the same companion data safely.

## What it provides

- Profile lifecycle: create a companion, hydrate a session, save a closeout,
  and retain its prompts and compact state.
- Memory and workspace: searchable memories, companion-to-companion inboxes,
  and small per-profile files stored outside Git.
- Structured data: companions propose typed stores; approved schemas validate
  every record. Shared projects let several companions work against a common,
  approval-controlled record set.
- Human control: bearer-credential access control, one-time enrollment invites,
  and TOTP-gated approval flows for sensitive changes.
- Integrations: a Streamable HTTP MCP server with OAuth or static bearer-token
  authentication, plus a small browser-based administration surface.

Data is deliberately simple and inspectable: SQLite for application data,
Markdown files for prompts, JSONL closeout logs, and plain files under the
configured data directory.

## Quick start: local development

Requirements: Python 3.12+ (the repository environment is supported) and
`pip`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn profile_os.api:app --reload
```

Open these local URLs:

- `http://127.0.0.1:8000/directory` — human-facing entry points
- `http://127.0.0.1:8000/settings` — TOTP-gated settings console (once set up)
- `http://127.0.0.1:8000/docs` — interactive OpenAPI documentation
- `http://127.0.0.1:8000/health` — health check

On first start, the service creates `data/` and seeds the example `sidra` and
`tara` profiles. Set `PROFILE_OS_DATA_DIR=/path/to/data` to use a different
location. Set `PROFILE_OS_SEED_DEMO_PROFILES=0` before first start to skip the
example profiles.

For a quick API check:

```bash
curl -sS http://127.0.0.1:8000/profiles
curl -sS -X POST http://127.0.0.1:8000/profiles/tara/session
```

Local authentication is off by default. That is convenient for development,
but it is not a production configuration.

## Secure backend setup

Enable access control before exposing the backend or using it with a remote
client. First create an admin credential locally; only its salted hash is
stored.

```bash
export ADMIN_SECRET='use-a-long-random-secret'
.venv/bin/python -m profile_os.bootstrap_admin --data-dir data --secret "$ADMIN_SECRET"
.venv/bin/python -m profile_os.enroll_totp --data-dir data
# Scan or enter the printed otpauth URI in an authenticator, then:
.venv/bin/python -m profile_os.enroll_totp --data-dir data --confirm 123456

PROFILE_OS_AUTH_ENABLED=1 .venv/bin/uvicorn profile_os.api:app
```

Use the bearer secret for protected API calls:

```bash
curl -H "Authorization: Bearer $ADMIN_SECRET" \
  http://127.0.0.1:8000/profiles
```

When enabled, all API routes except health and explicitly public,
TOTP/invite-based entry points require a credential with the appropriate
grant. Credentials belong to principals (people, applications, and
bridges), not to the companions themselves. See [ACCESS_CONTROL.md](ACCESS_CONTROL.md)
for the operation map, signed agent credentials, approvals, and enrollment.

## Docker and remote MCP

The Compose stack runs a private backend and an MCP service. Copy the template,
replace every `change-me-*` value with a long random secret, then start it:

```bash
cp .env.example .env
docker compose up --build
```

The backend is exposed on port 8000 and MCP on port 8080 by default. The stack
enables backend authentication and bootstraps a least-privilege bridge
credential automatically. For any public deployment, bind ports to loopback or
place the MCP service behind HTTPS; do not expose the backend administration
surface publicly.

The MCP endpoint is `POST`/`GET /mcp`. It supports OAuth (recommended) and a
static connector token for clients that cannot complete OAuth. Configure a
public HTTPS origin in `MCP_PUBLIC_BASE_URL` before connecting Claude.ai,
ChatGPT, or another remote MCP client.

See [MCP_CONNECTOR.md](MCP_CONNECTOR.md) for the environment variables, OAuth
flow, tunnel/HTTPS setup, smoke commands, and the complete tool list.

## Core concepts

| Concept | Purpose |
| --- | --- |
| Profile | A companion's identity, prompts, policy, compact state, and owned data. |
| Session | A bounded hydration packet for a companion's first turn: prompts, state, relevant memory, identity (when allowed), inbox indicator, and server time. |
| Memory | Searchable free-text events such as facts, preferences, decisions, and observations. |
| Closeout | Caller-supplied handoff data (`facts`, `texture`, `exchange`, and notes). The backend stores it; it never asks an LLM to summarize. |
| Dynamic store | A profile-scoped, typed record collection. Schemas are proposed then approved; writes are validated. |
| Project | A shared, approval-controlled typed record collection that multiple companions can join. |
| Approval | A pending prompt, store, or project decision. Sensitive approvals can require a live, single-use TOTP code. |

The main HTTP workflow is: create or choose a profile, call `POST
/profiles/{id}/session`, let the connected assistant work, then save memories,
files, records, and a `POST /profiles/{id}/closeout` before ending the session.

## Testing

Read [TESTING.md](TESTING.md) before diagnosing or changing tests. In
particular, use the repository interpreter:

```bash
.venv/bin/python -m pytest tests -q
```

Tests are local only: no network, LLM call, or API key is required.

## Documentation

- [API.md](API.md) — HTTP endpoints, request shapes, lifecycle, and data-store contract
- [ACCESS_CONTROL.md](ACCESS_CONTROL.md) — principals, grants, credentials, enrollment, TOTP, and approvals
- [MCP_CONNECTOR.md](MCP_CONNECTOR.md) — remote MCP transport, OAuth, connector configuration, and security boundary
- [TOOL_BRIDGE.md](TOOL_BRIDGE.md) — Python HTTP tool bridge for hosted assistants
- [ARCHITECTURE.md](ARCHITECTURE.md) — persistence and design decisions
- [UI_SPEC.md](UI_SPEC.md) — intended user-facing client experience
- [DEPLOY.md](DEPLOY.md) — current single-server deployment runbook
