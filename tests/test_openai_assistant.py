"""Local assistant REPL: fake-model conversations over a real in-process
backend. No `openai` package required — every test injects a fake client."""

import json
import sys
import types

from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.bridge import MEMORY_KINDS, TOOLS, ToolBridge
from profile_os.openai_assistant import ASSISTANT_TOOLS, AssistantSession, repl


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
    """Replays a script: each entry is a tool-call list or a text answer.
    Records every `messages` list it was sent."""

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


def _session(tmp_path, script, profile="tara"):
    app = create_app(data_dir=str(tmp_path / "data"))
    bridge = SpyBridge(app)
    session = AssistantSession(bridge, profile, client=FakeOpenAI(script),
                               printer=lambda *_: None)
    return app, bridge, session


def test_two_turns_remember_then_search(tmp_path):
    script = [
        [_tool_call("1", "remember", {"profile_id": "tara", "kind": "note",
                                      "content": "repl likes green tea"})],
        "Noted.",
        [_tool_call("2", "search_memories", {"profile_id": "tara",
                                             "query": "green tea"})],
        "You like green tea.",
    ]
    _, bridge, session = _session(tmp_path, script)
    session.start()
    assert session.send("remember I like green tea") == "Noted."
    assert session.send("what do I like?") == "You like green tea."
    # the search really hit the stored memory (tool result in history)
    tool_results = [m for m in session.messages if m["role"] == "tool"]
    assert "green tea" in tool_results[-1]["content"]
    assert bridge.executed == ["boot", "remember", "search_memories"]


def test_boot_called_once_and_seeds_system_prompt(tmp_path):
    _, bridge, session = _session(tmp_path, ["hi"])
    session.start()
    session.send("hello")
    assert bridge.executed.count("boot") == 1
    system = session.messages[0]
    assert system["role"] == "system"
    assert "'tara'" in system["content"]
    assert "SOURCE OF TRUTH" in system["content"]


def test_history_preserved_across_turns(tmp_path):
    fake = None

    script = ["one", "two"]
    app = create_app(data_dir=str(tmp_path / "data"))
    bridge = SpyBridge(app)
    fake = FakeOpenAI(script)
    session = AssistantSession(bridge, client=fake, printer=lambda *_: None)
    session.start()
    session.send("first message")
    session.send("second message")
    # second request contained the whole first exchange
    last_sent = fake.seen_messages[-1]
    contents = [m.get("content") for m in last_sent]
    assert "first message" in contents
    assert "one" in contents
    assert "second message" in contents


def test_exit_triggers_closeout(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"))
    bridge = SpyBridge(app)
    inputs = iter(["remember I tested the repl", "/exit", ""])
    script = ["ok, noted"]
    repl(bridge, "tara", client=FakeOpenAI(script),
         input_fn=lambda _prompt: next(inputs), printer=lambda *_: None)
    assert bridge.executed[-1] == "closeout"
    state = bridge.boot("tara")["compact_state"]
    assert "REPL session ended after 1 user turn" in state
    assert "remember I tested the repl" in state


def test_disallowed_tool_refused_not_executed(tmp_path):
    script = [
        [_tool_call("1", "add_record", {"profile_id": "tara",
                                        "store_name": "x",
                                        "data": {"item": "y"}})],
        "could not do that",
    ]
    _, bridge, session = _session(tmp_path, script)
    session.start()
    session.send("write a record")
    assert "add_record" not in bridge.executed
    tool_msg = [m for m in session.messages if m["role"] == "tool"][-1]
    assert "not allowed" in tool_msg["content"]


def test_lifecycle_and_store_write_tools_not_exposed():
    # propose_store IS exposed (companions may request structure), but
    # writes and lifecycle stay out: approval is a human admin act.
    for name in ("approve", "reject", "archive", "add_record"):
        assert name not in ASSISTANT_TOOLS
    assert "propose_store" in ASSISTANT_TOOLS


def test_remember_schema_has_kind_enum():
    remember = next(t for t in TOOLS if t["name"] == "remember")
    kind = remember["inputSchema"]["properties"]["kind"]
    assert kind["enum"] == ["decision", "fact", "failure_scar", "note",
                            "observation", "preference"] == MEMORY_KINDS


def test_invalid_kind_fed_back_and_retried(tmp_path):
    script = [
        [_tool_call("1", "remember", {"profile_id": "tara",
                                      "kind": "preference_or_setup",
                                      "content": "repl setup done"})],
        [_tool_call("2", "remember", {"profile_id": "tara", "kind": "note",
                                      "content": "repl setup done"})],
        "Stored as a note.",
    ]
    _, bridge, session = _session(tmp_path, script)
    session.start()
    assert session.send("remember my setup") == "Stored as a note."
    tool_msgs = [m for m in session.messages if m["role"] == "tool"]
    first = json.loads(tool_msgs[0]["content"])
    assert first["status"] == 422  # error fed back to the model, no abort
    assert "error" in first
    second = json.loads(tool_msgs[1]["content"])
    assert second["kind"] == "note"  # retry really stored the memory
    hits = bridge.search_memories("tara", "repl setup")
    assert any(e["kind"] == "note" for e in hits)


def test_fake_client_needs_no_openai_package(tmp_path):
    saved = {k: sys.modules.pop(k) for k in list(sys.modules)
             if k == "openai" or k.startswith("openai.")}
    try:
        _, _, session = _session(tmp_path, ["hello"])
        session.start()
        assert session.send("hi") == "hello"
        assert "openai" not in sys.modules
    finally:
        sys.modules.update(saved)
