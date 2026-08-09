# Life Career Truth MCP

`life_mcp` exposes the latest reviewed career snapshot from the local
`/home/andres/life` workspace as an authenticated, read-only MCP service.

The authority boundary is structural:

- Vértice working locally in Codex may update `life`, run its analyzer, review
  the diff, and publish a snapshot over SSH.
- Remote companions can search the published projection.
- The HTTP and MCP surfaces contain no publication or mutation operation.
- Raw documents, local paths, private evidence excerpts, and the private
  review artifacts never leave the home machine.

## Tools

- `career_source_status`
- `search_career_entities`
- `get_career_entity`
- `list_career_timeline`
- `get_career_provenance`

Every tool is annotated read-only, non-destructive, and idempotent.

## Local verification

```bash
.venv/bin/python -m pytest tests/test_life_mcp.py
.venv/bin/python -m life_mcp.publish_snapshot --validate-only \
  /home/andres/life/artifacts/jobdocs/single_source_of_truth.json
```

## Publication

After updating and reviewing the generated artifact in `life`:

```bash
scripts/publish_life_snapshot.sh
```

The script validates locally, then streams only the resolved JSON over the
existing SSH deployment channel. Inside the container, publication strips the
local `docs_root`, validates unique entity keys and categories, writes an
immutable content-addressed version, and atomically replaces `current.json`.
The publisher reports the new snapshot ID and previous snapshot ID.

There is deliberately no HTTP upload route and no MCP write tool.

## Production

- Service: `life-mcp` in this repository's Docker Compose deployment.
- Loopback port: `127.0.0.1:8091`.
- Intended URL: `https://life.datacodemath.com/mcp`.
- Persistent volume: `life-mcp-data`.
- Authentication: OAuth DCR + Authorization Code/PKCE, with a static bearer
  fallback for clients that cannot complete OAuth.

Production `.env` requires generated, independent values for:

```text
LIFE_MCP_PUBLIC_BASE_URL=https://life.datacodemath.com
LIFE_MCP_PORT=127.0.0.1:8091
LIFE_MCP_AUTH_REQUIRED=1
LIFE_MCP_CONNECTOR_TOKEN=...
LIFE_MCP_OAUTH_SIGNING_KEY=...
LIFE_MCP_ADMIN_SECRET=...
```

The connector and OAuth secrets authorize reads only. They cannot publish a
snapshot because no network publication endpoint exists.
