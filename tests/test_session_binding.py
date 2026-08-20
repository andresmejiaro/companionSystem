"""Session-bound companion locking: store logic + end-to-end enforcement."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from profile_os.mcp_server import MCPSettings, create_mcp_app
from profile_os.session_binding import SessionBindingStore

from tests.test_mcp_server import CONNECTOR_TOKEN, ORIGIN, PUBLIC_BASE, FakeBridge, _rpc


# --- store / evaluate unit tests -----------------------------------------

def _store(tmp_path, **kw):
    return SessionBindingStore(str(tmp_path / "b.sqlite3"), **kw)


def test_bind_reports_only_a_true_rebind(tmp_path):
    store = _store(tmp_path)
    assert store.bind("F1", "tara") is None          # new
    assert store.bind("F1", "tara") is None           # re-summon same profile
    assert store.bind("F1", "sidra") == "tara"        # rebind reports prior
    assert store.get("F1") == "sidra"


def test_evaluate_allows_unguarded_and_same_profile(tmp_path):
    store = _store(tmp_path)
    store.bind("F1", "tara")
    assert store.evaluate("F1", "discover_companions", "-").action == "allow"
    assert store.evaluate("F1", "send_message", "sidra").action == "allow"
    assert store.evaluate("F1", "remember", "tara").action == "allow"


def test_evaluate_blocks_cross_profile_write(tmp_path):
    store = _store(tmp_path)
    store.bind("F1", "tara")
    decision = store.evaluate("F1", "remember", "sidra")
    assert decision.action == "block"
    assert decision.kind == "mutation"
    assert decision.bound_profile == "tara"


def test_evaluate_reads_follow_the_gate(tmp_path):
    store = _store(tmp_path)
    store.bind("F1", "tara")
    assert store.evaluate("F1", "search_memories", "sidra").action == "block"
    store.set_strict_mode(False)  # revert to trusted
    assert store.evaluate("F1", "search_memories", "sidra").action == "allow"
    # Writes stay blocked regardless of the trust switch.
    assert store.evaluate("F1", "remember", "sidra").action == "block"


def test_switch_blocked_reports_only_a_real_strict_switch(tmp_path):
    store = _store(tmp_path)
    assert store.switch_blocked("F1", "tara") is None          # first summon
    store.bind("F1", "tara")
    assert store.switch_blocked("F1", "tara") is None           # same companion
    assert store.switch_blocked("F1", "sidra") == "tara"        # strict: blocked
    assert store.switch_blocked(None, "sidra") is None          # no fingerprint
    store.set_strict_mode(False)
    assert store.switch_blocked("F1", "sidra") is None          # trusted: allowed


def test_evaluate_fails_open_without_fingerprint_or_binding(tmp_path):
    store = _store(tmp_path)
    assert store.evaluate(None, "remember", "sidra").reason == "no-fingerprint"
    assert store.evaluate("unknown", "remember", "sidra").reason == "unbound"


def test_fingerprints_are_hashed_at_rest(tmp_path):
    import sqlite3
    path = tmp_path / "b.sqlite3"
    store = SessionBindingStore(str(path), fingerprint_key="secret-key")
    raw = "v1/super-secret-session-token"
    store.bind(raw, "tara")
    store.audit(fingerprint=raw, subject_hash=None, tool="remember",
                target_profile="sidra", decision="block")
    # Bindings still match by raw fingerprint...
    assert store.get(raw) == "tara"
    # ...but the raw value appears nowhere in the file.
    blob = sqlite3.connect(str(path)).execute(
        "SELECT group_concat(fingerprint) FROM bindings "
        "UNION ALL SELECT group_concat(fingerprint) FROM session_audit").fetchall()
    stored = " ".join(str(r[0]) for r in blob)
    assert raw not in stored
    assert "super-secret" not in stored


def test_prune_drops_stale_bindings(tmp_path):
    store = _store(tmp_path)
    store.bind("F1", "tara")
    assert store.prune(older_than_seconds=-1) == 1
    assert store.get("F1") is None


# --- end-to-end enforcement over /mcp ------------------------------------

def _client(bridge=None):
    # Isolate each client's binding store; the default path is a shared /tmp file.
    binding_file = str(Path(tempfile.gettempdir()) / f"bindings-{uuid.uuid4().hex}.sqlite3")
    settings = MCPSettings(
        auth_required=True,
        connector_tokens=[CONNECTOR_TOKEN],
        allowed_origins=[ORIGIN],
        public_base_url=PUBLIC_BASE,
        oauth_issuer=PUBLIC_BASE,
        oauth_signing_key="test-signing-key",
        session_binding_state_file=binding_file,
    )
    return TestClient(create_mcp_app(bridge=bridge or FakeBridge(), settings=settings))


def _headers(session: str | None = None, extra_openai: bool = True):
    h = {"Authorization": f"Bearer {CONNECTOR_TOKEN}", "Origin": ORIGIN,
         "Accept": "application/json"}
    if session is not None:
        h["x-openai-session" if extra_openai else "Mcp-Session-Id"] = session
    return h


def _call(client, name, arguments, headers, rid=1):
    return client.post("/mcp", json=_rpc(
        "tools/call", {"name": name, "arguments": arguments}, rid), headers=headers)


def _is_error(response) -> bool:
    return response.json()["result"].get("isError", False)


def test_summon_then_cross_write_is_blocked_same_profile_allowed():
    client = _client()
    h = _headers("v1/chatA")
    _call(client, "summon_companion", {"profile_id": "tara"}, h)

    same = _call(client, "remember", {"profile_id": "tara", "kind": "note", "content": "x"}, h)
    assert not _is_error(same)

    cross = _call(client, "remember", {"profile_id": "sidra", "kind": "note", "content": "x"}, h)
    assert _is_error(cross)
    err = cross.json()["result"]["structuredContent"]["error"]
    assert err["code"] == "session_bound"
    assert err["bound_profile"] == "tara"
    assert "send_message" in cross.json()["result"]["content"][0]["text"]


def test_concurrent_chats_bind_independently():
    client = _client()
    _call(client, "summon_companion", {"profile_id": "tara"}, _headers("v1/chatA"))
    _call(client, "summon_companion", {"profile_id": "sidra"}, _headers("v1/chatB"))

    # Each chat writes into its own companion without tripping the wall.
    a = _call(client, "remember", {"profile_id": "tara", "kind": "n", "content": "x"},
              _headers("v1/chatA"))
    b = _call(client, "remember", {"profile_id": "sidra", "kind": "n", "content": "x"},
              _headers("v1/chatB"))
    assert not _is_error(a) and not _is_error(b)
    # Cross-writes from chat B into tara are blocked.
    assert _is_error(_call(client, "remember",
                           {"profile_id": "tara", "kind": "n", "content": "x"},
                           _headers("v1/chatB")))


def test_strict_default_blocks_switching_companions_in_one_session():
    client = _client()
    h = _headers("v1/chatA")
    _call(client, "summon_companion", {"profile_id": "tara"}, h)
    # A re-summon of the same companion is fine.
    assert not _is_error(_call(client, "summon_companion", {"profile_id": "tara"}, h))
    # Switching to a different companion is blocked (one companion per session).
    switch = _call(client, "summon_companion", {"profile_id": "sidra"}, h)
    assert _is_error(switch)
    err = switch.json()["result"]["structuredContent"]["error"]
    assert err["code"] == "session_locked"
    assert err["bound_profile"] == "tara"
    # The block did not rebind: tara writes still pass, sidra writes still fail.
    assert not _is_error(_call(client, "remember",
                               {"profile_id": "tara", "kind": "n", "content": "x"}, h))
    assert _is_error(_call(client, "remember",
                           {"profile_id": "sidra", "kind": "n", "content": "x"}, h))


def test_trusted_mode_allows_switching_and_flips_the_wall():
    client = _client()
    client.app.state.bindings.set_strict_mode(False)  # revert to trusted
    h = _headers("v1/chatA")
    _call(client, "summon_companion", {"profile_id": "tara"}, h)
    assert not _is_error(_call(client, "summon_companion", {"profile_id": "sidra"}, h))  # rebind
    assert not _is_error(_call(client, "remember",
                               {"profile_id": "sidra", "kind": "n", "content": "x"}, h))
    assert _is_error(_call(client, "remember",
                           {"profile_id": "tara", "kind": "n", "content": "x"}, h))


def test_no_fingerprint_falls_open():
    client = _client()
    # No x-openai-session and no Mcp-Session-Id: advisory only, write allowed.
    r = _call(client, "remember", {"profile_id": "sidra", "kind": "n", "content": "x"},
              _headers(None))
    assert not _is_error(r)


def test_x_conv_id_header_binds_stock_cli_clients():
    # Stock Claude Code / Codex via mcp-remote stamp their own durable session
    # id into x-conv-id. Same enforcement as the native connectors.
    client = _client()
    h = {"Authorization": f"Bearer {CONNECTOR_TOKEN}", "Origin": ORIGIN,
         "Accept": "application/json", "x-conv-id": "597ed6bf-cli-session"}
    _call(client, "summon_companion", {"profile_id": "tara"}, h)
    assert not _is_error(_call(client, "remember",
                               {"profile_id": "tara", "kind": "n", "content": "x"}, h))
    assert _is_error(_call(client, "remember",
                           {"profile_id": "sidra", "kind": "n", "content": "x"}, h))


def test_fingerprint_priority_prefers_openai_over_conv_id():
    # If both are present the ChatGPT header wins; x-conv-id is the CLI fallback.
    client = _client()
    both = {"Authorization": f"Bearer {CONNECTOR_TOKEN}", "Origin": ORIGIN,
            "Accept": "application/json",
            "x-openai-session": "v1/chatA", "x-conv-id": "cli-xyz"}
    _call(client, "summon_companion", {"profile_id": "tara"}, both)
    # Binding is under the openai session; a request carrying only x-conv-id is
    # a different (unbound) conversation and falls open rather than matching.
    only_conv = {"Authorization": f"Bearer {CONNECTOR_TOKEN}", "Origin": ORIGIN,
                 "Accept": "application/json", "x-conv-id": "cli-xyz"}
    assert not _is_error(_call(client, "remember",
                               {"profile_id": "sidra", "kind": "n", "content": "x"}, only_conv))


def test_initialize_mints_mcp_session_id_and_claude_binds():
    client = _client()
    init = client.post("/mcp", json=_rpc("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}},
    ), headers={"Authorization": f"Bearer {CONNECTOR_TOKEN}", "Origin": ORIGIN,
                "Accept": "application/json"})
    session_id = init.headers.get("Mcp-Session-Id")
    assert session_id
    h = _headers(session_id, extra_openai=False)
    _call(client, "summon_companion", {"profile_id": "tara"}, h)
    assert _is_error(_call(client, "remember",
                           {"profile_id": "sidra", "kind": "n", "content": "x"}, h))
