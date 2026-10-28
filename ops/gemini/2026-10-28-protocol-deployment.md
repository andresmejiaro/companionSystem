# Gemini MCP protocol compatibility — 2026-10-28

Pre-change checkpoints: workstation FableCompanion `0f1f9c6`; VPS checkout
`1b0aa94`. Actual deployed MCP source was independently checkpointed in the
isolated working repository before edits.

Recent Gemini OAuth exchanges at 21:08:14, 21:08:41 and 21:09:28 UTC succeeded
(HTTP 200), followed by HTTP 400 on both POST and GET /mcp. In the deployed
endpoint GET 400 uniquely identified unsupported MCP-Protocol-Version. The
original access logs did not include the actual version value.

Changes:
- Accept 2025-11-25 alongside 2025-06-18 and 2025-03-26.
- Allow authenticated initialize requests to negotiate a supported version,
  even if their pre-negotiation version header is unfamiliar.
- Keep unsupported versions rejected on subsequent requests.
- Return the selected version in response headers, consistent with the
  initialize body and subsequent request version.
- Log rejected method and bounded protocol version without auth tokens,
  request bodies or OAuth query strings.

Built FROM profile-os-mcp:qol-20261027, replacing only mcp_server.py. This
preserves Gemini OAuth, inbox fetch semantics, evidence contract, and disabled
Tamagochi runtime. Backend remains profile-os-backend:qol-20261027.
Candidate MCP image: profile-os-mcp:gemini-protocol-20261028
Image manifest: sha256:b9405af061809cdee43d8b388459d257aebfabd1261121789878f5dc381f8497

Verification:
- Before patch: new compatibility tests 6 failed, 1 passed.
- After patch: compatibility + MCP/OAuth + bridge + inbox tests: 59 passed
  locally against the pinned source and 59 passed inside the candidate image.
- Authenticated public HTTPS initialize and tools/list: HTTP 200, 27 tools for
  each supported version; response header and body version agree.
- Authenticated HTTPS summon_companion(hilo) with 2025-11-25 succeeded; no
  inbox read or real message acknowledgement.
- Docker inspect: new MCP image healthy; pinned backend healthy.
- Actual Gemini client retry remains pending; do not claim end-to-end Gemini
  recovery solely from these independent probes.

Deploy (MCP only):
    cd /opt/profile-os
    docker compose -f docker-compose.yml -f releases/gemini-protocol-20261028/compose.json up -d --no-build --no-deps mcp

Rollback:
    cd /opt/profile-os
    docker compose -f docker-compose.yml -f releases/gemini-protocol-20261028/compose.rollback.json up -d --no-build --no-deps mcp

The source checkout now includes this protocol patch but is still not a full
reconstruction of the pinned QoL source. Build derivative images from this
release or reconcile all pinned changes before rebuilding the old checkout.

References:
https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
