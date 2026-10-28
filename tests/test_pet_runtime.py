import json
import asyncio
import time
from pathlib import Path

import httpx
import pytest

from profile_os.api import create_app
from profile_os.pet_runtime import PetResponsesRuntime, runtime_from_environment
from profile_os.pet_conversations import RuntimeOutcomeUnknown, RuntimeStopped
from profile_os.storage import Store


def setup_runtime(tmp_path, handler=None, identity=False):
    data = str(tmp_path / "data")
    store = Store(data)
    for profile in ("hilo", "limo"):
        store.create_profile(profile, profile.title(), f"canonical {profile} identity",
                             f"canonical {profile} surface")
    if handler is None:
        handler = lambda request: httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "A brief reply."}]}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(data_dir=data, do_seed=False, pet_runtime=object())
    issued = app.state.pet_devices.issue(["hilo"], identity_read=identity)
    runtime = PetResponsesRuntime(store, app.state.dynstores, app.state.pet_devices,
                                  "synthetic-server-secret", {"hilo": "test-model"},
                                  "https://provider.invalid/v1/responses", str(tmp_path / "id.txt"), client)
    (tmp_path / "id.txt").write_text("synthetic authorized identity")
    ident = {"principal_id": issued["principal_id"], "credential_id": issued["credential_id"]}
    return store, app, runtime, ident


def test_responses_flow_uses_canonical_context_and_keeps_reasoning(tmp_path):
    seen = []

    def provider(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body["store"] is False
        assert body["include"] == ["reasoning.encrypted_content"]
        assert body["model"] == "test-model"
        assert "canonical hilo identity" in body["instructions"]
        assert "canonical hilo surface" in body["instructions"]
        assert "synthetic authorized identity" in body["instructions"]
        assert body["tools"][0]["name"] == "search_memories"
        if len(seen) == 1:
            assert body["input"] == [
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "prior"},
                {"role": "user", "content": "hello"},
            ]
            return httpx.Response(200, json={"status": "completed", "output": [
                {"type": "reasoning", "id": "r", "summary": []},
                {"type": "reasoning", "id": "r2", "encrypted_content": "opaque"},
                {"type": "function_call", "name": "search_memories", "call_id": "c1",
                 "arguments": '{"query":"nothing","limit":3}'}]})
        assert any(item.get("type") == "reasoning" for item in body["input"])
        assert any(item.get("encrypted_content") == "opaque" for item in body["input"])
        assert any(item.get("type") == "function_call_output" for item in body["input"])
        return httpx.Response(200, json={"status": "completed", "output": [{
            "type": "message", "role": "assistant", "status": "completed", "content": [
            {"type": "output_text", "text": "Found nothing."}]}]})

    store, app, runtime, identity = setup_runtime(tmp_path, provider, identity=True)
    try:
        assert runtime.run("hilo", [{"text": "old", "reply_text": "prior"}], "hello",
                           identity, lambda: True) == "Found nothing."
        assert len(seen) == 2
        assert "synthetic-server-secret" not in json.dumps(seen)
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_tool_arguments_cannot_select_profile_or_mutate(tmp_path):
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "function_call",
            "name": "query_records", "call_id": "c1",
            "arguments": '{"store_name":"x","contains":null,"limit":1,"profile_id":"limo"}'}]})

    store, app, runtime, identity = setup_runtime(tmp_path, provider)
    try:
        with pytest.raises(RuntimeError):
            runtime.run("hilo", [], "hello", identity, lambda: True)
        assert len(calls) == 1
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_forbidden_mutation_tool_is_rejected(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "completed", "output": [{
            "type": "function_call", "name": "remember", "call_id": "mutate-1",
            "arguments": '{"text":"write this"}'}]})

    store, app, runtime, identity = setup_runtime(tmp_path, provider)
    before = store.db.execute("SELECT COUNT(*) n FROM memory_events").fetchone()["n"]
    try:
        with pytest.raises(RuntimeError, match="tool_not_allowed"):
            runtime.run("hilo", [], "hello", identity, lambda: True)
        assert len(calls) == 1
        assert store.db.execute("SELECT COUNT(*) n FROM memory_events").fetchone()["n"] == before
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_provider_transport_timeout_is_unknown_and_never_retried(tmp_path):
    calls = []

    def provider(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret provider detail")

    store, app, runtime, identity = setup_runtime(tmp_path, provider)
    try:
        with pytest.raises(RuntimeOutcomeUnknown):
            runtime.run("hilo", [], "hello", identity, lambda: True)
        assert len(calls) == 1
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_checkpoint_and_identity_grant_are_enforced(tmp_path):
    store, app, runtime, identity = setup_runtime(tmp_path)
    try:
        with pytest.raises(RuntimeStopped):
            runtime.run("hilo", [], "hello", identity, lambda: False)
        # identity file is not read without the profile-scoped identity grant.
        assert not runtime._authorized(identity, "hilo", "identity:read")
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_legacy_api_key_environment_cannot_enable_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("PET_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("PET_OPENAI_API_KEY", "synthetic")
    monkeypatch.setenv("PET_MODEL_HILO", "hilo-model")
    monkeypatch.delenv("PET_MODEL_LIMO", raising=False)
    store, app, runtime, identity = setup_runtime(tmp_path)
    try:
        configured = runtime_from_environment(store, app.state.dynstores,
                                              app.state.pet_devices)
        assert configured is None
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_close_disables_runtime_support(tmp_path):
    store, app, runtime, identity = setup_runtime(tmp_path)
    try:
        assert runtime.supports("hilo")
        runtime.close()
        assert not runtime.supports("hilo")
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_hydration_rechecks_boot_grant_and_does_not_create_stores(tmp_path):
    store, app, runtime, identity = setup_runtime(tmp_path)
    reads = []
    original_read_soul = store.read_soul
    store.read_soul = lambda profile: (reads.append(profile), original_read_soul(profile))[1]
    store_count = store.db.execute(
        "SELECT COUNT(*) n FROM dynamic_stores WHERE profile_id='hilo'").fetchone()["n"]
    checkpoints = 0

    def checkpoint():
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            app.state.pet_devices.revoke(identity["credential_id"])
        return True

    try:
        with pytest.raises(RuntimeStopped):
            runtime._hydrate("hilo", identity, checkpoint)
        assert reads == []
        assert store.db.execute(
            "SELECT COUNT(*) n FROM dynamic_stores WHERE profile_id='hilo'").fetchone()["n"] == store_count
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_full_hydration_reads_store_metadata_without_creating_continuity(tmp_path):
    store, app, runtime, identity = setup_runtime(tmp_path)
    before = store.db.execute(
        "SELECT COUNT(*) n FROM dynamic_stores WHERE profile_id='hilo'").fetchone()["n"]
    try:
        context = runtime._hydrate("hilo", identity, lambda: True)
        after = store.db.execute(
            "SELECT COUNT(*) n FROM dynamic_stores WHERE profile_id='hilo'").fetchone()["n"]
        assert context["data_sources"]["profile_stores"] == []
        assert after == before
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


@pytest.mark.parametrize("identity_read, revoke_at", [(False, 5), (True, 6)])
def test_boot_grant_is_rechecked_before_scoped_context_reads(tmp_path, identity_read, revoke_at, monkeypatch):
    store, app, runtime, identity = setup_runtime(tmp_path, identity=identity_read)
    grants = app.state.pet_devices.access.list_grants(identity["principal_id"])
    boot_grant = next(g for g in grants if g["operation"] == "boot" and g["profile_id"] == "hilo")
    reads = []
    original_read_text = Path.read_text
    def watched_read_text(path, *args, **kwargs):
        if str(path) == runtime.identity_file:
            reads.append(str(path))
        return original_read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", watched_read_text)
    checkpoints = 0

    def checkpoint():
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == revoke_at:
            app.state.pet_devices.access.revoke_grant(boot_grant["id"])
        return True

    try:
        with pytest.raises(RuntimeStopped):
            runtime._hydrate("hilo", identity, checkpoint)
        assert app.state.pet_devices.authorize(
            identity["principal_id"], "hilo",
            "identity:read" if identity_read else "records:read", identity["credential_id"])
        assert not app.state.pet_devices.authorize(
            identity["principal_id"], "hilo", "boot", identity["credential_id"])
        if identity_read:
            assert reads == []
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_tool_dispatch_rechecks_grant_after_provider_response(tmp_path):
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "completed", "output": [{
            "type": "function_call", "name": "search_memories", "call_id": "c1",
            "arguments": '{"query":"nothing","limit":3}'}]})

    store, app, runtime, identity = setup_runtime(tmp_path, provider)
    searches = []
    original_search = store.search
    store.search = lambda *args: (searches.append(args), original_search(*args))[1]
    checkpoints = 0

    def checkpoint():
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 7:
            app.state.pet_devices.revoke(identity["credential_id"])
        return True

    try:
        with pytest.raises(RuntimeStopped):
            runtime.run("hilo", [], "hello", identity, checkpoint)
        assert len(calls) == 1
        assert searches == []
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_incomplete_response_and_malformed_message_are_rejected(tmp_path):
    for index, (result, error) in enumerate((
        ({"status": "incomplete", "output": []}, "incomplete_provider_response"),
        ({"status": "completed", "output": [{"type": "message", "content": "bad"}]},
         "invalid_provider_message"),
        ({"status": "completed", "output": [{"type": "message", "role": "assistant",
          "status": "in_progress", "content": [{"type": "output_text", "text": "partial"}]}]},
         "invalid_provider_message"),
    )):
        store, app, runtime, identity = setup_runtime(
            tmp_path / f"case-{index}", lambda request, result=result: httpx.Response(200, json=result))
        try:
            with pytest.raises(RuntimeError, match=error):
                runtime.run("hilo", [], "hello", identity, lambda: True)
        finally:
            app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_aggregate_tool_call_limit_is_enforced(tmp_path):
    rounds = []

    def provider(request):
        rounds.append(request)
        count = (5, 5, 3)[len(rounds) - 1]
        output = [{"type": "function_call", "name": "search_memories",
                   "call_id": f"c{len(rounds)}-{i}",
                   "arguments": '{"query":"nothing","limit":1}'} for i in range(count)]
        return httpx.Response(200, json={"status": "completed", "output": output})

    store, app, runtime, identity = setup_runtime(tmp_path, provider)
    try:
        with pytest.raises(RuntimeError, match="provider_tool_call_limit"):
            runtime.run("hilo", [], "hello", identity, lambda: True)
        assert len(rounds) == 3
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_aggregate_input_limit_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr("profile_os.pet_runtime.MAX_TOTAL_INPUT_BYTES", 80)
    store, app, runtime, identity = setup_runtime(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="runtime_context_too_large"):
            runtime.run("hilo", [], "x" * 100, identity, lambda: True)
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_aggregate_input_limit_counts_replayed_provider_output(tmp_path, monkeypatch):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "reasoning", "id": "r", "encrypted_content": "x" * 7000},
            {"type": "function_call", "name": "search_memories", "call_id": "c1",
             "arguments": '{"query":"nothing","limit":1}'}]})

    monkeypatch.setattr("profile_os.pet_runtime.MAX_TOTAL_INPUT_BYTES", 16000)
    store, app, runtime, identity = setup_runtime(tmp_path, provider)
    try:
        with pytest.raises(RuntimeError, match="runtime_input_too_large"):
            runtime.run("hilo", [], "hello", identity, lambda: True)
        assert len(calls) == 1
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_total_deadline_cancels_drip_fed_response(tmp_path, monkeypatch):
    class DripStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.02)
                yield b" "

        async def aclose(self):
            return None

    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=DripStream())

    monkeypatch.setattr("profile_os.pet_runtime.TOTAL_SECONDS", 0.12)
    store, app, runtime, identity = setup_runtime(tmp_path, provider)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeOutcomeUnknown):
            runtime.run("hilo", [], "hello", identity, lambda: True)
        assert time.monotonic() - started < 0.8
        assert len(calls) == 1
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())


def test_deadline_prevents_second_local_tool_dispatch(tmp_path, monkeypatch):
    requests, searches = [], []
    def provider(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "function_call", "name": "search_memories", "call_id": "c1",
             "arguments": '{"query":"first","limit":1}'},
            {"type": "function_call", "name": "search_memories", "call_id": "c2",
             "arguments": '{"query":"second","limit":1}'}]})

    monkeypatch.setattr("profile_os.pet_runtime.TOTAL_SECONDS", 0.04)
    store, app, runtime, identity = setup_runtime(tmp_path, provider)
    def slow_search(profile, query, limit):
        searches.append(query)
        time.sleep(0.06)
        return []
    store.search = slow_search
    try:
        with pytest.raises(RuntimeOutcomeUnknown):
            runtime.run("hilo", [], "hello", identity, lambda: True)
        assert searches == ["first"]
        assert len(requests) == 1
    finally:
        app.state.store.close(); store.close(); asyncio.run(runtime.client.aclose())
