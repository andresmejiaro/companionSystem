# Deploy runbook: rumbo.datacodemath.com

Single-user Profile OS deployment on a Hetzner VPS, deployed 2026-07-09.

## Topology

- VPS: `62.238.55.207` (Hetzner CX22, Ubuntu 26.04), SSH key-only, `root`
- Domain: `rumbo.datacodemath.com` → A record → VPS IP
- Caddy (host-installed) terminates TLS on 443 and reverse-proxies to the
  `mcp` container on `127.0.0.1:8080`. The `backend` container
  (`127.0.0.1:8000`) is **not** exposed publicly — reach it only via SSH
  tunnel.
- Code lives at `/opt/profile-os` on the VPS, a plain `git clone` of
  `git@github.com:andresmejiaro/companionSystem.git` (public repo, cloned
  over HTTPS so no deploy key is needed).
- Data persists in the Docker named volume `profile-os_profile-os-data`.

## One-time setup (already done)

- `ufw`: only 22/80/443 open; `unattended-upgrades` enabled
- SSH: password auth disabled, key-only (`/etc/ssh/sshd_config.d/99-hardening.conf`)
- Docker Engine + compose plugin installed from Docker's `noble` apt repo
  (Ubuntu 26.04 is too new for Docker's own repo; pinned to 24.04's)
- Caddy installed from the official apt repo; `/etc/caddy/Caddyfile`:
  ```
  rumbo.datacodemath.com {
  	reverse_proxy 127.0.0.1:8080
  }
  ```
- `/opt/profile-os/.env` holds three generated secrets
  (`PROFILE_OS_MCP_BACKEND_BEARER`, `MCP_CONNECTOR_TOKEN`,
  `MCP_OAUTH_SIGNING_KEY` — each `secrets.token_urlsafe(48)`), plus
  `MCP_PUBLIC_BASE_URL=https://rumbo.datacodemath.com` and origin/redirect
  allowlists widened to include `claude.ai` and `chatgpt.com`/`openai.com`.
  **This file is never committed.**
- Backend port mapping is `127.0.0.1:8000:8000` / mcp `127.0.0.1:8080:8080`
  in `docker-compose.yml` via `.env`'s `PROFILE_OS_BACKEND_PORT` /
  `PROFILE_OS_MCP_PORT` — loopback only, no public admin surface.
- Admin credential bootstrapped once via
  `docker compose exec backend python -m profile_os.bootstrap_admin`.
  The plaintext secret was shown once and is not stored anywhere recoverable
  — if lost, bootstrap a new admin credential (it's idempotent per
  principal, adds a new credential to the same principal).
- Nightly backup cron installed (see below).

## Deploying an update

```bash
ssh root@62.238.55.207
cd /opt/profile-os
git pull
docker compose up -d --build
docker compose ps                       # both should show "healthy"
curl -s https://rumbo.datacodemath.com/health
```

`_migrate()` in `profile_os/access.py` runs idempotent `ALTER TABLE`s on
every backend start, so schema changes ship with normal deploys — no
separate migration step.

## Day-to-day operations

- Check status: `docker compose ps`
- Tail logs: `docker compose logs -f mcp` (or `backend`)
- Open the private `/demo` console (never exposed publicly):
  ```bash
  ssh -L 8000:127.0.0.1:8000 root@62.238.55.207
  # then open http://localhost:8000/demo in a local browser
  ```
- Mint an agent-enrollment invite (local CLI, never HTTP):
  ```bash
  docker compose exec backend python -m profile_os.mint_invite --data-dir /app/data
  ```

## Backups

- `/root/backup-profile-os.sh` tars the `profile-os-data` volume to
  `/root/backups/profile-os-data-<timestamp>.tar.gz`, pruning anything
  older than 14 days. Runs nightly at 03:00 via root's crontab.
- Pull a copy down: `scp root@62.238.55.207:/root/backups/profile-os-data-*.tar.gz .`
- Restore into a scratch directory to verify a backup:
  ```bash
  mkdir -p /tmp/restore-check && cd /tmp/restore-check
  tar xzf /root/backups/profile-os-data-<timestamp>.tar.gz
  ls   # expect profile_os.db and profiles/
  ```

## Secrets — where each one lives and what it's for

| Secret | Used by | Never goes into |
|---|---|---|
| `PROFILE_OS_MCP_BACKEND_BEARER` | `mcp` container → `backend` container, internal only | Connector setup screens or other public clients |
| `MCP_CONNECTOR_TOKEN` | Fallback bearer auth if a connector UI can't do OAuth | Prefer OAuth; only paste this if forced |
| `MCP_OAUTH_SIGNING_KEY` | Signs OAuth tokens the mcp service issues | Never leaves the server |
| Admin bootstrap secret | `/demo` console + admin API, via SSH tunnel only | Public internet and connector clients |

All four live only in `/opt/profile-os/.env` on the VPS (`chmod 600`) and
were generated with `secrets.token_urlsafe(48)`.

## Connecting a remote MCP client (Claude.ai / ChatGPT)

Server URL: `https://rumbo.datacodemath.com/mcp`. Full walkthrough in
[MCP_CONNECTOR.md](MCP_CONNECTOR.md). Prefer OAuth; fall back to
`MCP_CONNECTOR_TOKEN` bearer auth only if a connector's UI won't complete
OAuth. Both require a desktop/web settings UI — not available in the
Claude or ChatGPT mobile apps as of this deploy.

## Known limits (accepted for a single-user deploy)

- No CI/CD, no monitoring/alerting, no rate limiting beyond `/enroll`'s
  naive 5/min-per-IP cap, no Postgres migration (SQLite on a volume is
  right-sized for one user).
- Nonce replay-protection cache for signed-request auth is in-memory,
  reset on container restart — acceptable for a single-process server.
