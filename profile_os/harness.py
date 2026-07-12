"""Local end-to-end harness simulating a hosted external assistant.

Drives the ToolBridge through the same scripted tool sequence a hosted
Claude/ChatGPT session would perform — boot, remember, search, propose a
store, hit the expected 409 on an unapproved write, read audit, closeout —
printing every tool call and its outcome. It is an integration rehearsal,
not a chatbot: no LLM is called, no network beyond the configured backend,
and no auth is bypassed (401/403 from the backend abort the run loudly).

Approval is deliberately absent: the bridge exposes no approve/reject/
archive tool, so the harness demonstrates the 409 and moves on.

Run against a live local backend:

    PROFILE_OS_BRIDGE_BEARER=$BRIDGE_SECRET \
        python -m profile_os.harness --profile tara

Env (same as the bridge):
    PROFILE_OS_BRIDGE_BASE_URL  backend base URL (default http://127.0.0.1:8000)
    PROFILE_OS_BRIDGE_BEARER    bridge credential secret (required if the
                                backend runs with PROFILE_OS_AUTH_ENABLED=1)

Tests run the same flow in-process via `run_harness(ToolBridge(client=
fastapi.testclient.TestClient(app)))` — no real network.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from .bridge import ToolBridge, ToolBridgeError

# Tools this harness is allowed to invoke. Lifecycle tools (approve/reject/
# archive) are not bridge tools at all; keeping an explicit allowlist lets
# tests assert the harness never grows one by accident.
HARNESS_TOOLS = ("boot", "remember", "search_memories", "propose_store",
                 "add_record", "list_stores", "audit", "closeout")


class HarnessError(Exception):
    """The scripted flow could not complete; carries the failing step."""

    def __init__(self, step: str, cause: ToolBridgeError):
        self.step = step
        self.cause = cause
        hint = ""
        if cause.status_code == 401:
            hint = (" — missing/invalid bridge credential; set "
                    "PROFILE_OS_BRIDGE_BEARER to a valid secret")
        elif cause.status_code == 403:
            hint = (" — bridge credential lacks the grant for this "
                    "operation/profile; grant it via AccessControl")
        super().__init__(f"step {step!r} failed: {cause}{hint}")


def _call(bridge, log, printer, step, tool, arguments):
    printer(f"-> {tool}({arguments})")
    try:
        result = bridge.call(tool, arguments)
    except ToolBridgeError as e:
        printer(f"   !! {e.status_code}: {e.detail}")
        log.append({"step": step, "tool": tool, "arguments": arguments,
                    "status": e.status_code, "error": e.detail})
        raise HarnessError(step, e)
    printer("   ok")
    log.append({"step": step, "tool": tool, "arguments": arguments,
                "status": "ok", "result": result})
    return result


def run_harness(bridge: ToolBridge, profile_id: str = "tara",
                printer=print) -> dict:
    """Execute the scripted assistant flow; returns {calls, summary}.

    Raises HarnessError on any unexpected backend refusal (401/403/…).
    The pre-approval add_record is the one call where an error is the
    *expected* outcome (409) — anything else there is a failure too.
    """
    log: list[dict] = []
    call = lambda step, tool, args: _call(bridge, log, printer, step, tool, args)

    store_name = f"harness_{uuid.uuid4().hex[:8]}"
    schema = {"fields": {"item": {"type": "string", "required": True}}}

    call("boot", "boot", {"profile_id": profile_id})
    call("remember", "remember",
         {"profile_id": profile_id, "kind": "note",
          "content": f"harness rehearsal note {store_name}",
          "tags": ["harness"]})
    hits = call("search", "search_memories",
                {"profile_id": profile_id, "query": store_name})
    if not any(store_name in e["content"] for e in hits):
        raise AssertionError("remembered note not found by search")

    call("propose", "propose_store",
         {"profile_id": profile_id, "name": store_name,
          "purpose": "harness rehearsal store", "schema": schema})

    # Expected refusal: the store is pending, so a write must 409.
    printer(f"-> add_record (pre-approval, expecting 409)")
    got_409 = False
    try:
        bridge.call("add_record", {"profile_id": profile_id,
                                   "store_name": store_name,
                                   "data": {"item": "x"}})
    except ToolBridgeError as e:
        if e.status_code in (401, 403):
            raise HarnessError("add_record_pre_approval", e)
        got_409 = e.status_code == 409
        printer(f"   expected refusal: {e.status_code}: {e.detail}")
    if not got_409:
        raise AssertionError("pre-approval add_record did not return 409")
    log.append({"step": "add_record_pre_approval", "tool": "add_record",
                "status": 409, "expected": True})

    printer("   note: approval is intentionally NOT a bridge tool — an "
            "admin approves via the demo console or API with "
            "stores:approve; the bridge credential is operational only.")

    call("list_stores", "list_stores", {"profile_id": profile_id})
    call("audit", "audit", {"profile_id": profile_id})
    call("closeout", "closeout",
         {"profile_id": profile_id,
          "facts": f"Rehearsed bridge tool flow; store {store_name} left pending for admin review.",
          "texture": "Routine integration rehearsal; no approval was attempted.",
          "exchange": "User: Run the harness.\nAssistant: Rehearsal completed.",
          "notes": "harness rehearsal complete"})

    summary = (f"Harness OK for profile {profile_id!r}: "
               f"{len([c for c in log if c['status'] == 'ok'])} tool calls "
               f"succeeded, 1 expected 409 on pre-approval write to "
               f"{store_name!r} (left pending; approval is admin-only and "
               "not exposed through the bridge). No LLM, no approval, no "
               "auth bypass.")
    printer(summary)
    return {"calls": log, "summary": summary, "store_name": store_name}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Scripted assistant rehearsal over the ToolBridge.")
    ap.add_argument("--profile", default="tara")
    args = ap.parse_args(argv)
    bridge = ToolBridge()
    try:
        run_harness(bridge, args.profile)
    except HarnessError as e:
        print(f"HARNESS FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
