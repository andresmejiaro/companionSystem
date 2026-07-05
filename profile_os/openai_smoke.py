"""Minimal OpenAI tool-calling smoke runner over the ToolBridge.

The real-model counterpart of `profile_os.harness`: instead of a scripted
tool sequence, an actual OpenAI model decides which bridge tools to call.
The runner loops — send messages, execute any requested tools via
ToolBridge, append results, repeat — until the model answers in plain
text, which is printed.

Boundaries (mirrors the harness):
  * Only tools in SMOKE_TOOLS are exposed/executed; lifecycle tools
    (approve/reject/archive) are not bridge tools at all.
  * Auth is untouched: the bridge sends its bearer, the backend
    authorizes. 401/403 abort loudly.
  * Secrets never enter the prompt or the logs — OPENAI_API_KEY and
    PROFILE_OS_BRIDGE_BEARER stay in env; tool arguments/results are the
    only things printed.

Run against a live local backend:

    OPENAI_API_KEY=... PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET \
        python -m profile_os.openai_smoke --profile tara "remember I tested this"

Env:
    OPENAI_API_KEY              OpenAI credential (required)
    OPENAI_SMOKE_MODEL          model id (default gpt-4o-mini)
    PROFILE_OS_BRIDGE_BASE_URL  backend base URL (default http://127.0.0.1:8000)
    PROFILE_OS_BRIDGE_BEARER    bridge credential secret (required if the
                                backend runs with PROFILE_OS_AUTH_ENABLED=1)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from openai import OpenAI

from .bridge import TOOLS, ToolBridge, ToolBridgeError

DEFAULT_MODEL = "gpt-4o-mini"
MAX_ROUNDS = 10

# Tools the smoke runner will expose to and execute for the model. Same
# operational-only stance as the harness allowlist.
SMOKE_TOOLS = ("boot", "remember", "search_memories", "list_stores",
               "get_store", "query_records", "audit", "closeout")

SYSTEM_PROMPT = (
    "You are a hosted assistant driving Assistant Profile OS for profile "
    "{profile_id!r} via tools. First call `boot` for that profile. Use "
    "`remember` to store anything the user asks you to remember, and "
    "`search_memories` to verify or recall it. When done, answer the user "
    "in plain text summarizing what you did and found."
)


def _openai_tools():
    return [{"type": "function",
             "function": {"name": t["name"],
                          "description": t["description"],
                          "parameters": t["inputSchema"]}}
            for t in TOOLS if t["name"] in SMOKE_TOOLS]


def _execute(bridge: ToolBridge, name: str, arguments: dict,
             printer) -> str:
    """Run one tool call; returns the JSON string fed back to the model."""
    printer(f"-> {name}({arguments})")
    if name not in SMOKE_TOOLS:
        printer("   !! refused: not an allowed smoke tool")
        return json.dumps({"error": f"tool {name!r} is not allowed"})
    try:
        result = bridge.call(name, arguments)
    except ToolBridgeError as e:
        printer(f"   !! {e.status_code}: {e.detail}")
        if e.status_code in (401, 403):
            raise
        return json.dumps({"error": e.detail, "status": e.status_code})
    printer("   ok")
    return json.dumps(result)


def run_smoke(bridge: ToolBridge, prompt: str, profile_id: str = "tara",
              model: str = DEFAULT_MODEL, client: OpenAI | None = None,
              printer=print) -> str:
    """Drive one model conversation to completion; returns the final text."""
    client = client or OpenAI()
    messages = [
        {"role": "system",
         "content": SYSTEM_PROMPT.format(profile_id=profile_id)},
        {"role": "user", "content": prompt},
    ]
    tools = _openai_tools()

    for _ in range(MAX_ROUNDS):
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=tools)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""
        messages.append({"role": "assistant", "content": msg.content,
                         "tool_calls": [tc.model_dump()
                                        for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            arguments = json.loads(tc.function.arguments or "{}")
            output = _execute(bridge, tc.function.name, arguments, printer)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": output})
    raise RuntimeError(f"model did not finish within {MAX_ROUNDS} rounds")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Real-model smoke run over the ToolBridge.")
    ap.add_argument("--profile", default="tara")
    ap.add_argument("--model",
                    default=os.environ.get("OPENAI_SMOKE_MODEL",
                                           DEFAULT_MODEL))
    ap.add_argument("prompt", help="user message, e.g. 'remember I tested this'")
    args = ap.parse_args(argv)

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 1

    bridge = ToolBridge()
    try:
        answer = run_smoke(bridge, args.prompt, args.profile, args.model)
    except ToolBridgeError as e:
        print(f"SMOKE FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        bridge.close()
    print("\n=== final answer ===")
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
