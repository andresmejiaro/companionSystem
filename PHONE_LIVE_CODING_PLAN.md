# Phone Live-Coding Plan

Date: 2026-08-14  
Status: superseded on 2026-09-20 by the deployed browser-first workstation in
[`BROWSER_CODING.md`](BROWSER_CODING.md)

The CC Pocket design below is retained as research history. It is not the
active implementation because locked-down library computers require a plain
browser path with no client installation.

## Goal

Live-code from a phone against an always-on Linux server while the home computer is off. The phone experience should be a chat composer with normal autocorrect, voice input, readable diffs, and explicit approvals—not a terminal as the primary interface.

The solution fails if the phone cannot invoke the real Companions MCP and start Hilo.

## Constraints

- The Linux server is the persistent execution host.
- Repositories and uncommitted work remain on the server.
- The agent runs as a stable, non-root Linux user, not in an ephemeral container.
- Server-side credentials must cover Codex, Companions MCP, Git, and the Rumbo deployment.
- Sessions survive phone disconnects and bridge/service restarts.
- Commands, file edits, diffs, and MCP approvals must be usable on a phone.
- Access is private and authenticated; no raw terminal or WebSocket is exposed publicly.
- SSH remains a recovery path. Its earlier problems—key mismatch and poor mobile typing—are fixable, so it is a soft fallback rather than a rejected option.

## Three viable lanes

1. **GPT/Codex:** CC Pocket controls a real Codex CLI/app-server on the Linux server. This is the preferred first experiment.
2. **Anthropic:** Claude Code Remote Control on the Linux server. This is the official Anthropic lane, independent of CC Pocket's Claude integration.
3. **Provider-neutral:** OpenCode Web. The harness is provider-neutral, although the selected model may still require a subscription, API key, or local inference.

## Preferred architecture: CC Pocket

```text
CC Pocket on phone
  -> Tailscale + CC Pocket bridge API key
  -> CC Pocket bridge on server, port 8765
  -> Codex app-server/CLI under a persistent dev user
     -> live repository checkouts
     -> ~/.codex/auth.json and ~/.codex/config.toml
     -> Companions production MCP
     -> Git/GitHub credentials
     -> Rumbo deployment SSH identity
```

CC Pocket consists of a Flutter mobile app and a Node/TypeScript bridge. It supports Codex and Claude sessions, session resume, command/file/MCP approvals, file browsing, diffs, Git operations, voice and image input, worktrees, network recovery, service persistence, Tailscale, and optional push notifications.

It is promising but young third-party software. The repository is MIT licensed and actively developed, but no independent security audit was found. Treat the bridge as a high-trust filesystem and command surface.

## Security settings

Do not accept CC Pocket's broad defaults for an internet-connected server. Use:

- Node.js 20.18.1 or newer.
- A dedicated non-root Linux user.
- `BRIDGE_HOST` bound only to the server's Tailscale address.
- A newly generated, high-entropy `BRIDGE_API_KEY`.
- `BRIDGE_ALLOWED_DIRS` restricted to exact repository parent directories.
- `BRIDGE_DISABLE_MDNS` enabled.
- Private/default Codex app-server mode initially; shared/managed mode is experimental.
- Push notifications disabled for the first trial. They can be enabled later if Firebase/FCM metadata is acceptable.
- Service secret file mode `0600`.
- No public firewall opening for port 8765.

The bridge's documented defaults include `0.0.0.0`, no API key, and `$HOME` filesystem scope; those are unsuitable here.

## Credential plan

### Network and bridge

- Enroll the server and phone in Tailscale.
- Generate a bridge API key specifically for this installation.
- Store it in the server service environment and the phone's secure storage.

### Codex

- Log ChatGPT/Codex in directly as the persistent server user.
- Let the server create its own `~/.codex/auth.json`; do not copy the home workstation token store.
- Keep Codex configuration in that same user's `~/.codex/config.toml`.

### Companions MCP

- Configure the production Companions server directly in server-side Codex.
- Prefer OAuth: `codex mcp login companions`, including the TOTP flow where required.
- Use a static bearer token only as a fallback.
- Mark the server required and keep write/MCP approvals visible to the phone client.

### Git and GitHub

- Create a dedicated server SSH key or use an authenticated `gh` installation.
- Give it only the repository permissions needed.
- Configure the intended Git author name and email for the server user.

### Rumbo deployment

- Preferred: create a server-specific deployment key and authorize it on the same target. It can then be revoked independently.
- Plan B: securely copy the exact workstation private key, only if the target cannot accept another key.
- Verify and pin `known_hosts` fingerprints.
- Put required deployment environment/secrets on the server with narrow permissions.
- Do not initially store the Rumbo private key on the phone. CC Pocket should trigger server-side operations as the dev user.

### Anthropic note

New CC Pocket Claude sessions require `ANTHROPIC_API_KEY`; Claude subscription login is not supported there. Therefore CC Pocket's Claude integration is outside the first trial. Official Claude Code Remote Control remains the separate Anthropic option.

## Rollout and validation

### 1. Establish the server identity first

Before installing CC Pocket, use SSH once to prove that the persistent dev user can:

- read and write a disposable repository;
- authenticate Codex with ChatGPT;
- fetch and push a disposable Git branch;
- SSH to the Rumbo deployment target;
- invoke Companions MCP and start Hilo.

This separates identity and permission failures from bridge failures.

### 2. Run the bridge manually

Use `npx @ccpocket/bridge@latest` for the trial and `npx @ccpocket/bridge@latest doctor` for diagnostics. Do not install the service yet. Bind only to Tailscale, set the API key and narrow allowed directories, pair the phone, and confirm port 8765 is not publicly reachable.

### 3. Exercise normal Codex work

From the phone:

- inspect a disposable repository;
- approve one safe command;
- make a tiny edit and inspect the diff;
- reject one proposed action;
- disconnect and reconnect;
- restart the bridge and resume the session.

### 4. Hilo hard gate

From the phone, ask: `Start session as Hilo.` Then require all of the following:

- the approval visibly identifies the real Companions `start_session` tool;
- the result identifies canonical profile `hilo` with `selection.settled=true`;
- Hilo reads its inbox;
- Hilo sends a harmless test message to Limo;
- the message is independently verified as delivered;
- `prepare_closeout` and `closeout` both complete.

If the MCP tool is missing, its approval is invisible, authentication cannot be repaired remotely, or the UI only produces imitation text, the CC Pocket trial fails.

### 5. Git and deployment gate

- Fetch the intended repositories.
- Push a disposable branch.
- Run a read-only Rumbo connectivity probe.
- Make one reversible staging change.
- Verify it outside the agent session and roll it back.

### 6. Make it persistent

Only after all gates pass, run `npx @ccpocket/bridge@latest setup` to install its Linux user systemd service. Verify restart/backoff behavior, the service `PATH` for Codex, reboot recovery, secret permissions, Tailscale-only binding, and SSH/tmux recovery.

## Acceptance criteria

- Works while the home computer is off.
- Phone autocorrect, predictive typing, or voice input is comfortable.
- Agent processes run as the intended server user.
- Sessions resume after disconnect and service restart.
- Diffs are legible and command/MCP approvals are explicit.
- The real Companions MCP starts Hilo and Hilo can read/send a message.
- Git commits use the correct identity.
- Rumbo can be deployed without manually copying terminal commands.
- Nothing beyond Tailscale is publicly exposed.
- SSH remains available for recovery.

## Rollback

- Stop and disable the bridge service.
- Revoke the bridge key and unpair the phone.
- Remove the Tailscale enrollment if desired.
- Revoke dedicated Git and deployment keys.
- Revoke the Companions OAuth grant or fallback token.
- Retain repositories and Codex configuration unless they were trial-only.

## Sources

- [CC Pocket repository](https://github.com/K9i-0/ccpocket)
- [CC Pocket architecture](https://k9i-0.github.io/ccpocket/architecture/)
- [CC Pocket bridge README](https://github.com/K9i-0/ccpocket/blob/main/packages/bridge/README.md)
- [CC Pocket security policy](https://github.com/K9i-0/ccpocket/blob/main/SECURITY.md)
- [CC Pocket privacy policy](https://github.com/K9i-0/ccpocket/blob/main/PRIVACY_POLICY.md)
- [CC Pocket on Google Play](https://play.google.com/store/apps/details?id=com.k9i.ccpocket)
- [CC Pocket on the App Store](https://apps.apple.com/us/app/cc-pocket-dev-agent-remote/id6759188790)
- [Official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp)
- [Official Claude Code Remote Control documentation](https://code.claude.com/docs/en/remote-control)
- [OpenCode Web documentation](https://opencode.ai/docs/web/)
- [OpenCode provider documentation](https://opencode.ai/docs/providers/)
