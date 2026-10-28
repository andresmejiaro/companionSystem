"""Isolated Responses test prototype; not an approved Tamagochi runtime.

Network requests obey a total deadline. Synchronous local context reads are
checked between operations but cannot be forcibly interrupted.
"""
from __future__ import annotations

import json
import time
import asyncio
from pathlib import Path

import httpx
from jsonschema import ValidationError, validate

from .pet_conversations import RuntimeOutcomeUnknown, RuntimeStopped
from .prompts import companion_contract

MAX_CONTEXT_BYTES = 96 * 1024
MAX_HISTORY_BYTES = 32 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 256 * 1024
MAX_TOTAL_TOOL_CALLS = 12
MAX_ROUNDS = 4
TOTAL_SECONDS = 90
CALL_SECONDS = 30

_TOOLS = [
    {"type": "function", "name": "search_memories",
     "description": "Search this companion's authorized memories.", "strict": True,
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "minLength": 1, "maxLength": 1000},
         "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
         "required": ["query", "limit"], "additionalProperties": False}},
    {"type": "function", "name": "query_records",
     "description": "Read records from one of this companion's approved stores.", "strict": True,
     "parameters": {"type": "object", "properties": {
         "store_name": {"type": "string", "minLength": 1, "maxLength": 100},
         "contains": {"type": ["string", "null"], "maxLength": 1000},
         "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
         "required": ["store_name", "contains", "limit"], "additionalProperties": False}},
]
_ARGS = {tool["name"]: tool["parameters"] for tool in _TOOLS}


class _BorrowedAsyncTransport(httpx.AsyncBaseTransport):
    """Let a request-scoped AsyncClient use an injected transport without owning it."""
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        return None


def _fits(value, maximum: int) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > maximum:
        raise RuntimeError("runtime_context_too_large")
    return encoded


def runtime_from_environment(store, dynstores, devices, identity_file=None):
    """Fail closed until a Codex/agy account-authenticated adapter is accepted.

    Andrés requires existing CLI account access, not an API key. Legacy PET_*
    provider settings deliberately cannot enable the isolated Responses class.
    Explicit runtime injection remains available for synthetic lifecycle tests.
    """
    return None


class PetResponsesRuntime:
    def __init__(self, store, dynstores, devices, api_key: str,
                 models: dict[str, str], endpoint: str,
                 identity_file: str | None = None, client=None):
        self.store, self.dynstores, self.devices = store, dynstores, devices
        self._api_key, self.models, self.endpoint = api_key, dict(models), endpoint
        self.identity_file = identity_file
        # Each request gets its own async client, so no pool crosses asyncio.run()
        # loops. Injected transports are borrowed; the client owns its wrapper.
        if client is None:
            self._transport = None
            self._owns_transport = True
            self.client = None
        else:
            if not isinstance(client, httpx.AsyncClient):
                raise TypeError("client must be an httpx.AsyncClient")
            self._transport = client._transport
            self.client = client
            self._owns_transport = False
        self._closed = False

    def close(self):
        """Mark the runtime closed; request-scoped clients close their transports."""
        if self._closed:
            return
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def supports(self, profile_id: str) -> bool:
        return not self._closed and profile_id in self.models and bool(self.models[profile_id])

    def _checkpoint(self, checkpoint):
        if not checkpoint():
            raise RuntimeStopped()

    def _authorized(self, identity, profile_id, operation):
        return self.devices.authorize(identity["principal_id"], profile_id,
                                      operation, identity["credential_id"])

    def _before_context_read(self, profile_id, identity, checkpoint, operation="boot"):
        self._checkpoint(checkpoint)
        if not self._authorized(identity, profile_id, "boot"):
            raise RuntimeStopped()
        if operation != "boot" and not self._authorized(identity, profile_id, operation):
            raise RuntimeStopped()

    def _hydrate(self, profile_id, identity, checkpoint):
        # Never touch canonical files or database-backed context before boot grant.
        self._before_context_read(profile_id, identity, checkpoint)
        boot = self.store.boot(profile_id)
        profile = boot["profile"]
        if profile.get("profile_kind") != "companion":
            raise RuntimeError("profile_unavailable")
        boot["profile"] = {k: profile[k] for k in
                           ("id", "display_name", "signature", "allowed_tools",
                            "memory_policy", "closeout_rules", "aliases", "profile_kind")
                           if k in profile}
        boot.pop("state_updated_at", None)
        boot["memories"] = [{"kind": e["kind"], "content": e["content"]}
                            for e in boot.pop("recent_memories", [])]
        boot["system_contracts"] = {"companion": companion_contract()}
        self._before_context_read(profile_id, identity, checkpoint)
        boot["soul"] = self.store.read_soul(profile_id)
        self._before_context_read(profile_id, identity, checkpoint)
        boot["recent_exchanges"] = self.store.recent_exchanges(profile_id, 4)
        self._before_context_read(profile_id, identity, checkpoint)
        boot["recent_session_log"] = self.store.recent_session_log(profile_id, 5)
        if self._authorized(identity, profile_id, "records:read"):
            self._before_context_read(profile_id, identity, checkpoint, "records:read")
            # DynamicStores.list() installs a continuity store as a side effect.
            # Hydration must remain read-only, so select current metadata directly.
            rows = self.dynstores.db.execute(
                "SELECT d.* FROM dynamic_stores d JOIN ("
                "SELECT name, MAX(version) AS version FROM dynamic_stores "
                "WHERE profile_id=? GROUP BY name) latest "
                "ON d.name=latest.name AND d.version=latest.version "
                "WHERE d.profile_id=? ORDER BY d.name", (profile_id, profile_id)
            ).fetchall()
            boot["data_sources"] = {"profile_stores": [
                {k: item[k] for k in ("name", "purpose", "status", "version", "schema")}
                for item in map(self.dynstores._to_dict, rows)]}
        else:
            boot["data_sources"] = {"profile_stores": []}
        # Identity file access has its own explicit profile-scoped grant.
        if self.identity_file and self._authorized(identity, profile_id, "identity:read"):
            self._before_context_read(profile_id, identity, checkpoint, "identity:read")
            path = Path(self.identity_file)
            boot["identity"] = path.read_text(encoding="utf-8") if path.is_file() else None
        else:
            boot["identity"] = None
        _fits(boot, MAX_CONTEXT_BYTES)
        return boot

    @staticmethod
    def _check_deadline(deadline):
        if time.monotonic() >= deadline:
            raise RuntimeOutcomeUnknown()

    def _dispatch(self, name, arguments, profile_id, identity, checkpoint, deadline):
        if name not in _ARGS:
            raise RuntimeError("tool_not_allowed")
        try:
            validate(instance=arguments, schema=_ARGS[name])
        except ValidationError as exc:
            raise RuntimeError("invalid_tool_arguments") from exc
        if name == "search_memories":
            operation = "search"
        elif name == "query_records":
            operation = "records:read"
        else:
            raise RuntimeError("tool_not_allowed")
        self._checkpoint(checkpoint)
        self._check_deadline(deadline)
        if not self._authorized(identity, profile_id, operation):
            raise RuntimeStopped()
        if name == "search_memories":
            result = self.store.search(profile_id, arguments["query"], arguments["limit"])
        else:
            result = self.dynstores.query_records(
                profile_id, arguments["store_name"], arguments["contains"],
                arguments["limit"])
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_RESULT_BYTES:
            raise RuntimeError("tool_result_too_large")
        return encoded.decode()

    @staticmethod
    async def _response_json(response, deadline):
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if time.monotonic() >= deadline:
                raise TimeoutError
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError("provider_response_too_large")
        return json.loads(body)

    def run(self, profile_id, history, text, identity, checkpoint) -> str:
        if self._closed:
            raise RuntimeError("runtime_closed")
        return asyncio.run(self._run_async(profile_id, history, text, identity, checkpoint))

    async def _run_async(self, profile_id, history, text, identity, checkpoint) -> str:
        if not self.supports(profile_id):
            raise RuntimeError("runtime_unavailable")
        started = time.monotonic()
        deadline = started + TOTAL_SECONDS
        context = self._hydrate(profile_id, identity, checkpoint)
        if not isinstance(history, list):
            raise RuntimeError("invalid_history")
        clean_history = []
        for item in history[-10:]:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not isinstance(item.get("reply_text"), str):
                raise RuntimeError("invalid_history")
            clean_history.extend((
                {"role": "user", "content": item["text"]},
                {"role": "assistant", "content": item["reply_text"]},
            ))
        history_bytes = _fits(clean_history, MAX_HISTORY_BYTES).decode()
        instructions = (
            "Follow the canonical companion profile, prompt sections, soul, and companion contract "
            "in the supplied context. The profile identity and surface policy are authoritative. "
            "Give a concise, warm reply. You may only read through the listed tools. Never claim "
            "to save, remember, send, mutate data, obtain approval, or use an unavailable tool. "
            "Do not ask for or expose another profile's information. Context JSON:\n" +
            _fits(context, MAX_CONTEXT_BYTES).decode())
        payload_input = json.loads(history_bytes) + [{"role": "user", "content": text}]
        _fits(payload_input, MAX_TOTAL_INPUT_BYTES)
        headers = {"Authorization": "Bearer " + self._api_key,
                   "Content-Type": "application/json"}
        total_tool_calls = 0
        aggregate_input_bytes = 0
        for round_number in range(MAX_ROUNDS):
            self._checkpoint(checkpoint)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeOutcomeUnknown()
            payload = {"model": self.models[profile_id], "instructions": instructions,
                       "input": payload_input, "tools": _TOOLS, "store": False,
                       "include": ["reasoning.encrypted_content"],
                       "max_output_tokens": 1200}
            try:
                round_input_bytes = (
                    len(_fits(payload_input, MAX_TOTAL_INPUT_BYTES)) +
                    len(_fits(instructions, MAX_CONTEXT_BYTES + 32768)))
                if aggregate_input_bytes + round_input_bytes > MAX_TOTAL_INPUT_BYTES:
                    raise RuntimeError("runtime_input_too_large")
                aggregate_input_bytes += round_input_bytes
                timeout = httpx.Timeout(min(CALL_SECONDS, remaining))
                async with asyncio.timeout(remaining):
                    transport = (httpx.AsyncHTTPTransport() if self._owns_transport
                                 else _BorrowedAsyncTransport(self._transport))
                    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
                        async with client.stream("POST", self.endpoint, headers=headers,
                                                 json=payload) as response:
                            data = await self._response_json(response, deadline)
            except httpx.HTTPStatusError as exc:
                raise RuntimeError("provider_rejected_request") from exc
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
                raise RuntimeOutcomeUnknown() from None
            self._checkpoint(checkpoint)
            if time.monotonic() >= deadline:
                raise RuntimeOutcomeUnknown()
            if not isinstance(data, dict) or data.get("status") != "completed":
                raise RuntimeError("incomplete_provider_response")
            output = data.get("output") if isinstance(data, dict) else None
            if not isinstance(output, list):
                raise RuntimeError("invalid_provider_response")
            if not all(isinstance(item, dict) and isinstance(item.get("type"), str) for item in output):
                raise RuntimeError("invalid_provider_response")
            for item in output:
                if item["type"] != "message":
                    continue
                if (item.get("role") != "assistant" or item.get("status") != "completed" or
                        not isinstance(item.get("content"), list)):
                    raise RuntimeError("invalid_provider_message")
                for part in item["content"]:
                    if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                        raise RuntimeError("invalid_provider_message")
                    if part["type"] == "output_text" and not isinstance(part.get("text"), str):
                        raise RuntimeError("invalid_provider_message")
            calls = [item for item in output if item.get("type") == "function_call"]
            if not calls:
                messages = [item for item in output if item.get("type") == "message"]
                if not messages or any(not isinstance(item.get("content"), list) for item in messages):
                    raise RuntimeError("invalid_provider_message")
                pieces = []
                for item in messages:
                    for part in item["content"]:
                        if part["type"] == "output_text":
                            pieces.append(part["text"])
                reply = "".join(pieces).strip()
                if not reply:
                    raise RuntimeError("empty_provider_reply")
                self._checkpoint(checkpoint)
                self._check_deadline(deadline)
                return reply
            total_tool_calls += len(calls)
            if total_tool_calls > MAX_TOTAL_TOOL_CALLS:
                raise RuntimeError("provider_tool_call_limit")
            if round_number == MAX_ROUNDS - 1:
                raise RuntimeError("provider_tool_round_limit")
            # Preserve every response item, including reasoning and encrypted content.
            payload_input.extend(output)
            for call in calls:
                name, call_id, raw = call.get("name"), call.get("call_id"), call.get("arguments")
                if not isinstance(call_id, str) or not isinstance(raw, str):
                    raise RuntimeError("invalid_provider_tool_call")
                try:
                    arguments = json.loads(raw)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("invalid_provider_tool_call") from exc
                self._check_deadline(deadline)
                result = self._dispatch(name, arguments, profile_id, identity, checkpoint, deadline)
                self._check_deadline(deadline)
                payload_input.append({"type": "function_call_output", "call_id": call_id,
                                      "output": result})
                if len(_fits(payload_input, MAX_TOTAL_INPUT_BYTES)) > MAX_TOTAL_INPUT_BYTES:
                    raise RuntimeError("runtime_input_too_large")
        raise RuntimeError("provider_tool_round_limit")
