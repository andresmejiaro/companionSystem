# Siro canonical profile ID — 2026-10-28

Migrated canonical profile ID vertice -> siro at Andrés's explicit request.
Display name remains Siro. The registry has one profile, not a duplicate.
Retired vertice ID remains a routing alias and explicit API redirect. Existing
worker job names, store names and artifact paths remain compatible.

Current deployed images:
- backend: profile-os-backend:siro-id-20261028
- MCP: profile-os-mcp:gemini-protocol-20261028-2

Backend candidate is FROM profile-os-backend:qol-20261027 and replaces only
storage.py, api.py and new migrate_profile_id.py. It preserves atomic inbox
fetching, evidence contract and disabled Tamagochi. MCP was not restarted;
the Gemini protocol fix remains deployed.

Implementation:
- Explicit retired-ID table, canonicalized route parameters before validation
  and authorization, preserving original signed URL/body bytes.
- Canonicalize message recipients and project membership/query ID inputs.
- Reject reuse of retired IDs when creating profiles.
- Offline migration uses a deferred-FK SQLite transaction, updates all schema
  profile ID columns and structured routing fields in JSON data/payloads,
  adds vertice alias, and renames the profile directory. On ordinary failure
  it rolls back SQL and compensates the directory move. Record IDs, narrative
  text, historical tags/audit detail and file bytes remain unchanged.
- Process/host crash between directory rename and DB commit requires offline
  recovery; migration is run with a mandatory consistent backup and stopped
  backend writer.

Verification:
- 125 relevant tests passed inside candidate image, including 5 migration
  tests: history/files/permissions/projects, old/new API requests, signed old
  paths, collision refusal, rollback before and after directory rename.
- 5 migration tests passed against the development checkout.
- Rehearsed on private server-side copy before production; integrity and
  foreign keys passed.
- Live backend stopped, SQLite backup taken, profile directory backed up, then
  migration applied. Live counts match rehearsal: memories25, closeouts22,
  store definitions9, records82, store-audit profile refs43/actors26, pending
  approvals17, sender refs44, recipient refs47. Five JSON routing records and
  five approval payloads updated. All table row counts preserved; all39 profile
  files hash-identical; SQLite integrity ok and no FK errors.
- Authenticated public MCP calls with both siro and vertice return canonical
  selection siro. File metadata accessible through both IDs; no real mail
  consumed or sent by verification. Both services healthy.

Evidence:
- MIGRATION.json in this release: exact migration counts and backup location.
- Private offline backup: /root/backups/siro-id-20261028-before
  (consistent profile_os.db and profiles/vertice tree).
- Rehearsal: /root/backups/siro-id-20261028-rehearsal
- Source/tests in this release and repository tests/test_profile_id_migration.py.

Redeploy current images:
    cd /opt/profile-os
    docker compose -f docker-compose.yml -f releases/siro-id-20261028/compose.json up -d --no-build --no-deps backend mcp

Image rollback compose.rollback.json restores pre-migration backend image,
not data. Reverting the ID requires a separate offline data rollback. Restore
the consistent DB and profile tree with writers stopped only if no later
writes must be retained; otherwise reconcile later changes first. An image
rollback alone removes compatibility routing for old-ID callers.

Do not rebuild the old checkout without reconciling pinned deployed source:
release Dockerfiles deliberately derive from the actual existing images.
