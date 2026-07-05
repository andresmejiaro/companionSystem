"""Secretary profile: prompt-only companion behavior. No seeded stores, no
seeded schemas, no secretary domain logic in the infrastructure — the
companion must decide via tools that it needs a structured store and
propose one, which then waits for human admin approval."""

import inspect
import json
import types

from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.bridge import ToolBridge
from profile_os.openai_assistant import AssistantSession


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
        self.seen_tools = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, tools):
        self.seen_tools.append([t["function"]["name"] for t in tools])
        step = self.script.pop(0)
        if isinstance(step, str):
            msg = types.SimpleNamespace(content=step, tool_calls=None)
        else:
            msg = types.SimpleNamespace(content=None, tool_calls=step)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)])


def _session(tmp_path, script):
    app = create_app(data_dir=str(tmp_path / "data"))
    bridge = SpyBridge(app)
    session = AssistantSession(bridge, "secretary",
                               client=FakeOpenAI(script),
                               printer=lambda *_: None)
    return bridge, session


TODO_SCHEMA = {"fields": {"task": {"type": "string", "required": True},
                          "due": {"type": "string", "required": False},
                          "status": {"type": "string", "required": True}}}


def test_boot_secretary_has_identity_but_no_stores_or_records(tmp_path):
    bridge, session = _session(tmp_path, [])
    boot = session.start()
    assert "You are Secretary" in boot["base_prompt"]
    assert bridge.list_stores("secretary") == []  # nothing pre-seeded
    assert boot.get("recent_memories", []) == []
    system = session.messages[0]["content"]
    assert "You are Secretary" in system
    assert "propose_store" in system  # contract explains the mechanism


def test_companion_decides_it_needs_a_store_and_proposes_one(tmp_path):
    script = [
        [_tool_call("1", "list_stores", {"profile_id": "secretary"})],
        [_tool_call("2", "propose_store",
                    {"profile_id": "secretary", "name": "todos",
                     "purpose": "recurring task list the user keeps",
                     "schema": TODO_SCHEMA})],
        "I've proposed a 'todos' store; it needs admin approval before use.",
    ]
    bridge, session = _session(tmp_path, script)
    session.start()
    reply = session.send("I keep giving you todos; track them properly")
    # decision flowed through tools: looked first, then proposed
    assert bridge.executed == ["boot", "list_stores", "propose_store"]
    # the proposal is real and pending — not live, not written to
    store = bridge.get_store("secretary", "todos")
    assert store["status"] == "pending"
    assert store["schema"] == TODO_SCHEMA
    assert "approval" in reply


def test_propose_store_offered_to_model_but_add_record_is_not(tmp_path):
    _, session = _session(tmp_path, ["ok"])
    session.start()
    session.send("hi")
    offered = session.client.seen_tools[-1]
    assert "propose_store" in offered
    assert "add_record" not in offered
    for name in ("approve", "reject", "archive"):
        assert name not in offered


def test_pending_store_rejects_record_writes(tmp_path):
    bridge, session = _session(tmp_path, [])
    session.start()
    bridge.propose_store("secretary", "todos", "tasks", TODO_SCHEMA)
    script = [
        [_tool_call("1", "add_record",
                    {"profile_id": "secretary", "store_name": "todos",
                     "data": {"task": "call bank", "status": "open"}})],
        "I can't write records; the store isn't approved yet.",
    ]
    session.client.script = script
    reply = session.send("add 'call bank' to the todos")
    assert "add_record" not in bridge.executed  # refused at the allowlist
    assert bridge.get_store("secretary", "todos")["status"] == "pending"
    assert "approved" in reply


def test_no_secretary_domain_logic_in_infrastructure():
    import profile_os.assistant_server as server
    import profile_os.openai_assistant as assistant
    import profile_os.openai_smoke as smoke
    for module in (server, assistant, smoke):
        src = inspect.getsource(module)
        assert "Secretary" not in src
        assert "todo" not in src.lower()


def test_seed_creates_no_secretary_stores_or_schemas(tmp_path):
    from profile_os.seed import seed
    from profile_os.storage import Store
    store = Store(str(tmp_path / "data"))
    seed(store)
    profile = next(p for p in store.list_profiles()
                   if p["id"] == "secretary")
    assert profile["display_name"] == "Secretary"
    bridge_view = create_app(data_dir=str(tmp_path / "data"))
    b = ToolBridge(client=TestClient(bridge_view))
    assert b.list_stores("secretary") == []
