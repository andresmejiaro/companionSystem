"""Token-only session-bound companion locking: store logic + end-to-end."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from profile_os.mcp_server import MCPSettings, create_mcp_app
from profile_os.session_binding import SessionBindingStore

from tests.test_mcp_server import CONNECTOR_TOKEN, ORIGIN, PUBLIC_BASE, FakeBridge, _rpc


# --- store / policy unit tests -------------------------------------------

def _store(tmp_path, **kw):
    return SessionBindingStore(str(tmp_path / "b.sqlite3"), **kw)


def test_summon_mints_a_token_and_binds():
    import tempfile as _t
    store = SessionBindingStore(str(Path(_t.gettempdir()) / f"{uuid.uuid4().hex}.sqlite3"))
    sd = store.evaluate_summon(None, "tara")
    assert sd.action == "allow" and sd.reason == "bind" and sd.token
    # Same-profile write with the token is allowed; cross is blocked.
    assert store.evaluate_call(sd.token, "remember", "tara").action == "allow"
    cross = store.evaluate_call(sd.token, "remember", "sidra")
    assert cross.action == "block" and cross.reason == "cross" and cross.bound_profile == "tara"


def test_missing_token_is_fail_closed_in_strict_open_in_trusted(tmp_path):
    store = _store(tmp_path)
    d = store.evaluate_call(None, "remember", "sidra")
    assert d.action == "block" and d.reason == "no-session"
    store.set_strict_mode(False)
    d = store.evaluate_call(None, "remember", "sidra")
    assert d.action == "allow" and d.reason == "advisory"


def test_switch_blocked_in_strict_rebinds_in_trusted(tmp_path):
    store = _store(tmp_path)
    token = store.evaluate_summon(None, "tara").token
    # Same companion re-summon keeps the token.
    again = store.evaluate_summon(token, "tara")
    assert again.reason == "resummon" and again.token == token
    # Switching to a different companion is blocked in strict mode.
    blocked = store.evaluate_summon(token, "sidra")
    assert blocked.action == "block" and blocked.reason == "switch-blocked"
    assert blocked.bound_profile == "tara"
    # Trusted mode rebinds instead.
    store.set_strict_mode(False)
    rb = store.evaluate_summon(token, "sidra")
    assert rb.action == "allow" and rb.reason == "rebind" and rb.token == token
    assert store.evaluate_call(token, "remember", "sidra").action == "allow"
    assert store.evaluate_call(token, "remember", "tara").action == "block"


def test_reads_follow_the_trust_switch(tmp_path):
    store = _store(tmp_path)
    token = store.evaluate_summon(None, "tara").token
    assert store.evaluate_call(token, "search_memories", "sidra").action == "block"
    store.set_strict_mode(False)
    assert store.evaluate_call(token, "search_memories", "sidra").action == "allow"
    # Cross writes stay blocked regardless of the switch.
    assert store.evaluate_call(token, "remember", "sidra").action == "block"


def test_unguarded_and_no_target_always_allowed(tmp_path):
    store = _store(tmp_path)
    assert store.evaluate_call(None, "discover_companions", "-").action == "allow"
    assert store.evaluate_call(None, "send_message", "sidra").action == "allow"


def test_tokens_are_hashed_at_rest(tmp_path):
    path = tmp_path / "b.sqlite3"
    store = SessionBindingStore(str(path), fingerprint_key="secret-key")
    token = store.evaluate_summon(None, "tara").token
    store.audit(token=token, subject_hash=None, tool="remember",
                target_profile="sidra", decision="cross")
    assert store.evaluate_call(token, "remember", "tara").action == "allow"
    blob = sqlite3.connect(str(path)).execute(
        "SELECT group_concat(token_digest) FROM sessions "
        "UNION ALL SELECT group_concat(token_digest) FROM session_events").fetchall()
    stored = " ".join(str(r[0]) for r in blob)
    assert token not in stored


def test_prune_drops_stale_sessions(tmp_path):
    store = _store(tmp_path)
    store.evaluate_summon(None, "tara")
    assert store.prune(older_than_seconds=-1) == 1


# --- end-to-end over /mcp ------------------------------------------------

def _client(bridge=None):
    binding_file = str(Path(tempfile.gettempdir()) / f"bindings-{uuid.uuid4().hex}.sqlite3")
    settings = MCPSettings(
        auth_required=True, connector_tokens=[CONNECTOR_TOKEN],
        allowed_origins=[ORIGIN], public_base_url=PUBLIC_BASE,
        oauth_issuer=PUBLIC_BASE, oauth_signing_key="test-signing-key",
        session_binding_state_file=binding_file,
    )
    return TestClient(create_mcp_app(bridge=bridge or FakeBridge(), settings=settings))


def _headers():
    return {"Authorization": f"Bearer {CONNECTOR_TOKEN}", "Origin": ORIGIN,
            "Accept": "application/json"}


def _call(client, name, arguments, token=None, rid=1):
    args = dict(arguments)
    if token is not None:
        args["session_token"] = token
    return client.post("/mcp", json=_rpc(
        "tools/call", {"name": name, "arguments": args}, rid), headers=_headers())


def _is_error(response):
    return response.json()["result"].get("isError", False)


def _summon(client, profile, token=None):
    r = _call(client, "summon_companion", {"profile_id": profile}, token=token)
    return r


def _token_of(response):
    return response.json()["result"]["structuredContent"].get("session_token")


def test_summon_returns_token_and_locks_conversation():
    client = _client()
    r = _summon(client, "tara")
    token = _token_of(r)
    assert token and token.startswith("st_")
    assert not _is_error(_call(client, "remember",
                               {"profile_id": "tara", "kind": "n", "content": "x"}, token=token))
    cross = _call(client, "remember", {"profile_id": "sidra", "kind": "n", "content": "x"}, token=token)
    assert _is_error(cross)
    assert cross.json()["result"]["structuredContent"]["error"]["code"] == "session_bound"


def test_guarded_call_without_token_is_blocked_in_strict():
    client = _client()
    r = _call(client, "remember", {"profile_id": "tara", "kind": "n", "content": "x"})
    assert _is_error(r)
    assert r.json()["result"]["structuredContent"]["error"]["code"] == "session_token_required"


def test_switch_blocked_same_resummon_ok():
    client = _client()
    token = _token_of(_summon(client, "tara"))
    assert not _is_error(_summon(client, "tara", token=token))  # re-summon same
    switch = _summon(client, "sidra", token=token)
    assert _is_error(switch)
    assert switch.json()["result"]["structuredContent"]["error"]["code"] == "session_locked"


def test_two_conversations_are_independent():
    client = _client()
    t1 = _token_of(_summon(client, "tara"))
    t2 = _token_of(_summon(client, "sidra"))
    assert t1 != t2
    assert not _is_error(_call(client, "remember", {"profile_id": "tara", "kind": "n", "content": "x"}, token=t1))
    assert not _is_error(_call(client, "remember", {"profile_id": "sidra", "kind": "n", "content": "x"}, token=t2))
    assert _is_error(_call(client, "remember", {"profile_id": "sidra", "kind": "n", "content": "x"}, token=t1))


def test_tools_advertise_session_token_argument():
    client = _client()
    r = client.post("/mcp", json=_rpc("tools/list"), headers=_headers())
    tools = r.json()["result"]["tools"]
    remember = next(t for t in tools if t["name"] == "remember")
    assert "session_token" in remember["inputSchema"]["properties"]
