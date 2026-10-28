# Tamagochi disabled-runtime deployment — 2026-10-17

Authorized by Andrés's “deploy”. Backend deployed to the existing VPS using
release `tamagochi-20261017-disabled-1`. AI execution is disabled; no provider
API key or Codex/agy conversation runtime was activated. Voice remains disabled.

## Isolation and acceptance

Built from the exact previously running backend image
`sha256:f94317e3865594e5b90a16db67236b5b5f7d2fa52eaec7ee5479f9063d3c2258`.
Compared live `api.py` and `access.py` against the local files: only reviewed
Tamagochi additions differed. Overlaid these plus `pet_api.py`,
`pet_conversations.py`, `pet_devices.py`, `pet_devices_cli.py`, and
`pet_runtime.py`. No git pull, commit, dependency upgrade, remote source edit,
or unrelated local changes were included. Existing live code was preserved.

Release image: `profile-os-backend:tamagochi-20261017-disabled-1`.
Rollback image: `profile-os-backend:before-tamagochi-20261017`.
Release context, SHA-256 manifest, Compose overrides, container baseline and
deployment-result JSON are under
`/opt/profile-os/releases/tamagochi-20261017-disabled-1` on the server.
The old backend command was preserved verbatim in the override; environment
comparison found no existing values changed. `PET_RUNTIME_ENABLED=0` is explicit
in the image and release override. Environment factory also fails closed.

Pre-switch container tests used temporary data and `--network none`:
**170 passed, one dependency deprecation warning, in 56.52 seconds**. Covered
pet runtime/auth/lifecycle, API/auth/access/TOTP and existing MCP server tests.
Container command used its installed `python -m pytest` rather than the local
repository venv; production image is Python 3.12.

Created an online SQLite backup and checked its integrity before switching:
`/app/data/backups/pre-tamagochi-20261017-disabled-1.sqlite3` inside the persistent
backend volume, permissions 0600. Backup remains only on the server.

## Live verification

- Backend, MCP and Life services healthy; only backend container recreated.
- Existing authenticated backend profile listing succeeded; unauthenticated
  listing returned 401. Profile content was not printed.
- Pet capabilities without auth or with the broad bridge credential: 401.
- A five-minute deployment-only device credential obtained capabilities with
  both flags false; text submission returned 503 without model execution.
- The smoke credential was revoked and subsequent access returned 401. Its
  revoked registry record and test conversation metadata remain for audit;
  no accepted turn or companion memory was created.
- Public HTTPS `/health`: 200.
- Connected `discover_companions` succeeded after switch, exercising MCP and
  its existing backend bridge. MCP and Life container IDs were unchanged.

Smoke script: `ops/tamagochi/verify_disabled_deployment.py`, also stored in the
server release directory. It does not print secrets or private profile data.

## Restart / rollback

The remote git working tree still represents the previous source plus its
pre-existing dirty work. Preserve the pinned release override when restarting;
plain `docker compose up --build` would rebuild from that older source.

```sh
cd /opt/profile-os
docker compose -f docker-compose.yml -f releases/tamagochi-20261017-disabled-1/compose.release.json up -d --no-build --no-deps --wait backend
```

Rollback the backend image if needed:

```sh
cd /opt/profile-os
docker compose -f docker-compose.yml -f releases/tamagochi-20261017-disabled-1/compose.rollback.json up -d --no-build --no-deps --wait backend
```

Pet schema additions are additive; an image rollback need not restore the
database or discard subsequent data. Do not restore the backup over live data
without a separate recovery decision. Codex/agy runtime adapter and voice remain
future work; client HTTP integration is separately owned by Limo.
