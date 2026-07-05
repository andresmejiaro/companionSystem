"""Assistant web server: fake-model conversations over a real in-process
backend, plus static-frontend and no-secret checks."""

import json
import types

from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.assistant_server import WEB_DIR, create_assistant_app
from profile_os.bridge import ToolBridge

FAKE_OPENAI_KEY = "sk-super-secret-test-key"
FAKE_BEARER = "bridge-bearer-test-secret"


class SpyBridge(ToolBridge):
    """Records every executed tool name (boot() and call() paths)."""

    def __init__(self, app):
        super().__init__(client=TestClient(app), bearer=FAKE_BEARER)
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


def _ui(tmp_path, script, profile="tara"):
    backend = create_app(data_dir=str(tmp_path / "data"))
    bridge = SpyBridge(backend)
    fake = FakeOpenAI(script)
    app = create_assistant_app(profile, bridge=bridge, client=fake)
    return TestClient(app), bridge, fake


def test_page_and_static_assets_load(tmp_path):
    ui, _, _ = _ui(tmp_path, [])
    r = ui.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Profile OS Assistant" in r.text
    assert ui.get("/static/app.js").status_code == 200
    assert ui.get("/static/style.css").status_code == 200


def test_start_boots_once_and_is_idempotent(tmp_path):
    ui, bridge, _ = _ui(tmp_path, [])
    r = ui.post("/api/start")
    assert r.status_code == 200
    body = r.json()
    assert body["started_now"] is True
    assert body["profile_id"] == "tara"
    assert body["display_name"] == "Tara"
    # second start is a no-op, no re-boot
    assert ui.post("/api/start").json()["started_now"] is False
    assert bridge.executed.count("boot") == 1


def test_message_requires_start(tmp_path):
    ui, _, _ = _ui(tmp_path, ["hi"])
    assert ui.post("/api/message", json={"text": "hi"}).status_code == 409


def test_message_preserves_history_and_returns_reply(tmp_path):
    script = [
        [_tool_call("1", "remember", {"profile_id": "tara", "kind": "note",
                                      "content": "web ui likes green tea"})],
        "Noted.",
        "You like green tea.",
    ]
    ui, bridge, fake = _ui(tmp_path, script)
    ui.post("/api/start")
    r = ui.post("/api/message", json={"text": "remember I like green tea"})
    assert r.json()["reply"] == "Noted."
    r = ui.post("/api/message", json={"text": "what do I like?"})
    assert r.json()["reply"] == "You like green tea."
    # second request carried the whole first exchange
    contents = [m.get("content") for m in fake.seen_messages[-1]]
    assert "remember I like green tea" in contents
    assert "Noted." in contents
    assert bridge.executed == ["boot", "remember"]
    assert ui.get("/api/status").json()["user_turns"] == 2


def test_tool_events_surfaced_for_debug_panel(tmp_path):
    script = [
        [_tool_call("1", "search_memories", {"profile_id": "tara",
                                             "query": "tea"})],
        "nothing found",
    ]
    ui, _, _ = _ui(tmp_path, script)
    ui.post("/api/start")
    events = ui.post("/api/message",
                     json={"text": "search"}).json()["tool_events"]
    assert any("search_memories" in e for e in events)


def test_closeout_updates_compact_state(tmp_path):
    ui, bridge, _ = _ui(tmp_path, ["ok"])
    ui.post("/api/start")
    ui.post("/api/message", json={"text": "hello"})
    r = ui.post("/api/closeout", json={"notes": "web ui session done"})
    assert r.status_code == 200 and r.json()["closed"] is True
    assert bridge.executed[-1] == "closeout"
    state = bridge.boot("tara")["compact_state"]
    assert "1 user turn" in state and "hello" in state
    # session is finished: further messages / closeouts refused
    assert ui.post("/api/message", json={"text": "x"}).status_code == 409
    assert ui.post("/api/closeout", json={}).status_code == 409
    assert ui.get("/api/status").json()["closed"] is True


def test_disallowed_tool_still_refused(tmp_path):
    script = [
        [_tool_call("1", "add_record", {"profile_id": "tara",
                                        "store_name": "x",
                                        "data": {"item": "y"}})],
        "could not do that",
    ]
    ui, bridge, _ = _ui(tmp_path, script)
    ui.post("/api/start")
    r = ui.post("/api/message", json={"text": "write a record"})
    assert r.status_code == 200
    assert "add_record" not in bridge.executed
    assert any("refused" in e for e in r.json()["tool_events"])


def test_no_secrets_in_page_or_api_responses(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.setenv("PROFILE_OS_BRIDGE_BEARER", FAKE_BEARER)
    ui, _, _ = _ui(tmp_path, ["hello"])
    pages = [ui.get("/").text, ui.get("/static/app.js").text,
             ui.get("/static/style.css").text,
             ui.post("/api/start").text,
             ui.post("/api/message", json={"text": "hi"}).text,
             ui.get("/api/status").text]
    for text in pages:
        assert FAKE_OPENAI_KEY not in text
        assert FAKE_BEARER not in text


def test_web_sources_contain_no_env_secret_names():
    # static files must not even reference the secret env vars
    for name in ("index.html", "app.js", "style.css"):
        src = (WEB_DIR / name).read_text()
        assert "sk-" not in src
        assert "PROFILE_OS_BRIDGE_BEARER=" not in src
