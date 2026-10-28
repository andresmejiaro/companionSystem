# Browser coding workstation

The phone/library path is a browser-only workstation hosted on the existing
VPS. `code-server` runs as the unprivileged `andres-dev` user, listens only on
`127.0.0.1:8443`, and is exposed by Caddy at
`https://rumbo.datacodemath.com/code/`. No additional public port is opened.
The existing Profile OS administrator TOTP protects the route through Caddy
forward authentication. A successful code creates a secure, HTTP-only,
same-site cookie scoped to `/code` for two hours. code-server has no separate
password and remains reachable only through loopback and that gate.

The browser provides the editor, terminal, Git, and compiler. OpenCode v2 is
installed for the same user with a read-only `reviewer` agent. Its policy denies
all capabilities by default, permits source inspection and language-server
queries, and permits only `git status`, `git diff`, and `git log` shell commands.
It cannot edit, build, commit, or push.

## Files

- `ops/browser-coding/code-server.yaml`: loopback-only code-server settings.
- `ops/browser-coding/code-server@andres-dev.override.conf`: systemd hardening.
- `ops/browser-coding/Caddyfile.fragment`: TOTP forward-auth routing.
- `ops/browser-coding/reviewer.md`: read-only OpenCode reviewer policy.
- `ops/browser-coding/opencode.jsonc`: makes that reviewer the default agent.

## Verification

On the VPS:

```bash
systemctl is-active code-server@andres-dev
ss -ltn | grep 127.0.0.1:8443
curl -I https://rumbo.datacodemath.com/code/
caddy validate --config /etc/caddy/Caddyfile
```

From an external browser, open the HTTPS URL, log in, and use the integrated
terminal. The disposable `browser-smoke` repository demonstrates the intended
human-owned loop: edit `answer.c`, run `make test`, commit, push, then run
`opencode --agent reviewer` to inspect the diff. Provider login is deliberately
performed interactively by the owner; no model credential is copied from root,
another machine, or a companion service.

## Remaining account gates

The infrastructure does not silently borrow credentials. Before real work, the
owner must authenticate Git for the intended remote and connect an OpenCode
provider from the `andres-dev` session. Validate each with a disposable branch
before granting access to production repositories.

## Rollback

Remove the `/code` Caddy handlers, reload Caddy, then disable
`code-server@andres-dev`. The service is isolated from existing Profile OS,
forum, Life, and Vesper processes.
