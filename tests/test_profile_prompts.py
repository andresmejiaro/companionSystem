"""Profile identity survival: Sidra and Tara must stay distinct companions
over the same generic infrastructure — identity from the backend boot
payload, tool discipline from the shared contract, none of it hardcoded in
the assistant server."""

import inspect
import json
import types

from fastapi.testclient import TestClient

import profile_os.assistant_server as assistant_server_module
from profile_os import prompts
from profile_os.api import create_app
from profile_os.bridge import MEMORY_KINDS, ToolBridge
from profile_os.openai_assistant import AssistantSession, build_system_prompt


class SpyBridge(ToolBridge):
    """Records every executed tool name (boot() and call() paths)."""

    def __init__(self, app):
        super().__init__(client=TestClient(app))
        self.executed = []

    def boot(self, profile_id):
        self.executed.append("boot")
        return super().boot(profile_id)

    def closeout(self, profile_id, notes, new_state):
        self.executed.append("closeout")
        return super().closeout(profile_id, notes, new_state)

    def call(self, name, arguments):
        self.executed.append(name)
        return super().call(name, arguments)


def _tool_call(i, name, args):
    tc = types.SimpleNamespace(
        id=i, function=types.SimpleNamespace(name=name,
                                             arguments=json.dumps(args)))
    tc.model_dump = lambda: {"id": tc.id, "type": "function",
                             "function": {"name": name,
                                          "arguments": tc.function.arguments}}
    return tc


class FakeOpenAI:
    """Replays a script: each entry is a tool-call list or a text answer."""

    def __init__(self, script):
        self.script = list(script)
        self.seen_messages = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, tools):
        self.seen_messages.append([dict(m) for m in messages])
        step = self.script.pop(0)
        if isinstance(step, str):
            msg = types.SimpleNamespace(content=step, tool_calls=None)
        else:
            msg = types.SimpleNamespace(content=None, tool_calls=step)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)])


def _session(tmp_path, script, profile):
    app = create_app(data_dir=str(tmp_path / "data"))
    bridge = SpyBridge(app)
    session = AssistantSession(bridge, profile, client=FakeOpenAI(script),
                               printer=lambda *_: None)
    return bridge, session


def test_boot_sidra_includes_sidra_identity_prompt(tmp_path):
    bridge, session = _session(tmp_path, [], "sidra")
    boot = session.start()
    assert "You are Sidra" in boot["base_prompt"]
    assert "task contract" in boot["role_prompt"]
    system = session.messages[0]["content"]
    assert "You are Sidra" in system
    assert "🍎🦫🪡✂️" in system
    assert "Tara" not in system  # no cross-profile bleed


def test_boot_tara_includes_tara_identity_prompt(tmp_path):
    bridge, session = _session(tmp_path, [], "tara")
    boot = session.start()
    assert "You are Tara" in boot["base_prompt"]
    assert "food tracking" in boot["role_prompt"].lower()
    system = session.messages[0]["content"]
    assert "You are Tara" in system
    assert "never invent nutrition" in system.lower()
    assert "Sidra" not in system


def test_system_prompt_includes_shared_tool_contract(tmp_path):
    contract = prompts.tool_contract()
    assert "SOURCE OF TRUTH" in contract
    assert "decision, fact, failure_scar, note, observation, preference" \
        in contract
    for profile in ("sidra", "tara"):
        _, session = _session(tmp_path, [], profile)
        session.start()
        assert contract in session.messages[0]["content"]


def test_contract_kind_list_matches_bridge_enum():
    listed = "decision, fact, failure_scar, note, observation, preference"
    assert listed in prompts.tool_contract()
    assert [k.strip() for k in listed.split(",")] == MEMORY_KINDS


def test_old_memory_question_searches_before_answering(tmp_path):
    script = [
        [_tool_call("1", "search_memories", {"profile_id": "sidra",
                                             "query": "migration"})],
        "Yes — scar on record: check migration chains before deleting.",
    ]
    bridge, session = _session(tmp_path, script, "sidra")
    session.start()
    reply = session.send("do we have any scars about migrations?")
    # search happened before any answer, and hit the seeded scar
    assert bridge.executed == ["boot", "search_memories"]
    tool_result = [m for m in session.messages if m["role"] == "tool"][-1]
    assert "migration" in tool_result["content"]
    assert "scar" in reply


def test_remember_uses_valid_kind_enum(tmp_path):
    script = [
        [_tool_call("1", "remember", {"profile_id": "tara",
                                      "kind": "preference",
                                      "content": "prefers oat milk"})],
        "Stored.",
    ]
    bridge, session = _session(tmp_path, script, "tara")
    session.start()
    assert session.send("remember I prefer oat milk") == "Stored."
    stored = bridge.search_memories("tara", "oat milk")
    assert stored and stored[0]["kind"] in MEMORY_KINDS
    assert stored[0]["kind"] == "preference"


def test_closeout_still_works_per_profile(tmp_path):
    for profile in ("sidra", "tara"):
        bridge, session = _session(tmp_path / profile, ["ok"], profile)
        session.start()
        session.send("hello")
        session.close("prompt-layer test session")
        assert bridge.executed[-1] == "closeout"
        assert "1 user turn" in bridge.boot(profile)["compact_state"]


def test_assistant_server_has_no_hardcoded_identity():
    for module in (assistant_server_module,):
        src = inspect.getsource(module)
        assert "Sidra" not in src
        assert "Tara" not in src


def test_openai_assistant_template_has_no_hardcoded_identity():
    import profile_os.openai_assistant as m
    src = inspect.getsource(m)
    assert "Sidra" not in src and "Tara" not in src


def test_build_system_prompt_identity_comes_only_from_boot():
    boot = {"base_prompt": "IDENTITY-FROM-BACKEND",
            "role_prompt": "ROLE-FROM-BACKEND",
            "compact_state": "STATE-FROM-BACKEND"}
    out = build_system_prompt("anyprofile", boot)
    for marker in boot.values():
        assert marker in out
    assert prompts.tool_contract() in out
