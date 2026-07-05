"""Minimal local assistant REPL for one profile, over the ToolBridge.

Extends `profile_os.openai_smoke` from one-shot to conversational: boot
the profile once, build a system prompt from the boot payload (compact
state + profile prompts), then loop on stdin — each user message runs the
same OpenAI tool-calling loop, with tool calls executed through
ToolBridge and results fed back. History is kept for the whole session.
`/exit` closes the session with a `closeout` (user-supplied note, or an
auto-derived one).

This is a real-model *local test*, not a product: no server, no
deployment, no admin lifecycle tools (approve/reject/archive are not even
bridge tools), and no store writes in this slice (propose_store /
add_record are not exposed). Secrets stay in env and are never printed;
a backend 401/403 aborts the session with a clear message.

Run against a live local backend:

    OPENAI_API_KEY=... PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET \
        python -m profile_os.openai_assistant --profile tara

Env:
    OPENAI_API_KEY              OpenAI credential (required for real runs)
    OPENAI_SMOKE_MODEL          model id (default gpt-4o-mini)
    PROFILE_OS_BRIDGE_BASE_URL  backend base URL (default http://127.0.0.1:8000)
    PROFILE_OS_BRIDGE_BEARER    bridge credential secret (required if the
                                backend runs with PROFILE_OS_AUTH_ENABLED=1)
"""

from __future__ import annotations

import argparse
import os
import sys

from . import prompts
from .bridge import ToolBridge, ToolBridgeError
from .openai_smoke import (DEFAULT_MODEL, SMOKE_TOOLS, _openai_tools,
                           _resolve_client, run_tool_loop)

# Smoke allowlist plus `propose_store`: a companion may *decide* it needs a
# structured store and propose one, but proposals stay pending until an
# admin approves — and record writes (add_record) remain unexposed, so the
# model can never write to a store in this slice. No lifecycle tools.
ASSISTANT_TOOLS = SMOKE_TOOLS + ("propose_store",)

# Layered system prompt. Identity (base/role prompts) comes ONLY from the
# backend boot payload — nothing profile-specific may be hardcoded here.
SYSTEM_TEMPLATE = """\
You are the assistant for Assistant Profile OS profile {profile_id!r}, \
running in a local session.

{tool_contract}
{base_prompt}
{role_prompt}
## Current compact state
{compact_state}
"""


def build_system_prompt(profile_id: str, boot: dict) -> str:
    """Assemble: shared tool contract + base prompt + role prompt +
    compact state. base/role come from the boot payload (backend is the
    source of truth); the tool contract is shared across all profiles."""
    return SYSTEM_TEMPLATE.format(
        profile_id=profile_id,
        tool_contract=prompts.tool_contract(),
        base_prompt=boot.get("base_prompt") or "",
        role_prompt=boot.get("role_prompt") or "",
        compact_state=boot.get("compact_state") or "(none)")


class AssistantSession:
    """One booted profile conversation: system prompt + running history.

    `client` is any openai.OpenAI-compatible object (tests pass fakes);
    the `openai` package is imported only when it is omitted.
    """

    def __init__(self, bridge: ToolBridge, profile_id: str = "tara",
                 model: str = DEFAULT_MODEL, client=None, printer=print):
        self.bridge = bridge
        self.profile_id = profile_id
        self.model = model
        self.client = _resolve_client(client)
        self.printer = printer
        self.messages: list[dict] = []
        self.user_turns = 0

    def start(self) -> dict:
        """Boot the profile (once) and seed the system prompt."""
        boot = self.bridge.boot(self.profile_id)
        self.messages = [{"role": "system",
                          "content": build_system_prompt(self.profile_id,
                                                         boot)}]
        return boot

    def send(self, text: str) -> str:
        """One user turn: run the tool loop, return the assistant reply."""
        self.messages.append({"role": "user", "content": text})
        self.user_turns += 1
        return run_tool_loop(self.bridge, self.client, self.model,
                             self.messages, _openai_tools(ASSISTANT_TOOLS),
                             printer=self.printer, allowed=ASSISTANT_TOOLS)

    def close(self, notes: str | None = None) -> dict:
        """Closeout with the given note, or one derived from the session."""
        auto = f"Local REPL session ended after {self.user_turns} user turn(s)."
        last = next((m["content"] for m in reversed(self.messages)
                     if m["role"] == "user"), "")
        new_state = auto if not last else f"{auto} Last topic: {last[:120]}"
        return self.bridge.closeout(self.profile_id, notes or auto, new_state)


def repl(bridge: ToolBridge, profile_id: str = "tara",
         model: str = DEFAULT_MODEL, client=None,
         input_fn=input, printer=print) -> AssistantSession:
    """Interactive loop; returns the (closed-out) session."""
    session = AssistantSession(bridge, profile_id, model, client, printer)
    boot = session.start()
    printer(f"[booted profile {profile_id!r}: "
            f"{boot.get('profile', {}).get('display_name', profile_id)}; "
            f"{len(boot.get('recent_memories', []))} recent memories. "
            "Type /exit to close out.]")
    while True:
        try:
            line = input_fn("you> ")
        except EOFError:
            line = "/exit"
        line = line.strip()
        if not line:
            continue
        if line == "/exit":
            try:
                note = input_fn("closeout note (enter for auto): ").strip()
            except EOFError:
                note = ""
            session.close(note or None)
            printer("[session closed out]")
            return session
        reply = session.send(line)
        printer(f"assistant> {reply}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Local real-model assistant REPL over the ToolBridge.")
    ap.add_argument("--profile", default="tara")
    ap.add_argument("--model",
                    default=os.environ.get("OPENAI_SMOKE_MODEL",
                                           DEFAULT_MODEL))
    args = ap.parse_args(argv)

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 1

    bridge = ToolBridge()
    try:
        repl(bridge, args.profile, args.model)
    except ToolBridgeError as e:
        hint = ""
        if e.status_code == 401:
            hint = " — missing/invalid PROFILE_OS_BRIDGE_BEARER"
        elif e.status_code == 403:
            hint = " — bridge credential lacks the grant for this operation"
        print(f"ASSISTANT ABORTED: {e}{hint}", file=sys.stderr)
        return 1
    except (RuntimeError, KeyboardInterrupt) as e:
        if isinstance(e, KeyboardInterrupt):
            print("\n[interrupted — no closeout]", file=sys.stderr)
        else:
            print(f"ASSISTANT ABORTED: {e}", file=sys.stderr)
        return 1
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
