"""JSON Schemas for Profile OS tool outputs.

The MCP server publishes these for successful ``structuredContent`` results.
The local bridge also publishes them for hosted-assistant runners, though it
returns raw backend payloads rather than MCP result envelopes.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

MEMORY_KINDS = [
    "decision",
    "fact",
    "failure_scar",
    "note",
    "observation",
    "preference",
]

JSON_OBJECT: dict[str, Any] = {"type": "object"}
STRING_OR_NULL: dict[str, Any] = {
    "anyOf": [{"type": "string"}, {"type": "null"}],
}
NUMBER_OR_NULL: dict[str, Any] = {
    "anyOf": [{"type": "number"}, {"type": "null"}],
}


def array_of(item_schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": deepcopy(item_schema)}


def mcp_items(item_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"items": array_of(item_schema)},
        "required": ["items"],
    }


IDENTITY = {
    "type": "object",
    "properties": {"content": {"type": "string"}},
    "required": ["content"],
}

PROFILE = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "display_name": {"type": "string"},
        "description": {"type": "string"},
        "allowed_tools": array_of({"type": "string"}),
        "memory_policy": JSON_OBJECT,
        "closeout_rules": {"type": "string"},
        "created_at": {"type": "number"},
    },
    "required": ["id", "display_name"],
}

MEMORY_EVENT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "profile_id": {"type": "string"},
        "kind": {"type": "string", "enum": MEMORY_KINDS},
        "content": {"type": "string"},
        "tags": array_of({"type": "string"}),
        "created_at": {"type": "number"},
    },
    "required": ["id", "profile_id", "kind", "content", "tags"],
}

# Session hydration is model context, not a storage export. IDs, tags,
# timestamps, and profile ids stay on memory lookup/mutation tools.
HYDRATION_MEMORY = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": MEMORY_KINDS},
        "content": {"type": "string"},
    },
    "required": ["kind", "content"],
}

CLOSEOUT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "profile_id": {"type": "string"},
        "notes": {"type": "string"},
        "facts": {"type": "string"},
        "texture": {"type": "string"},
        "exchange": {"type": "string"},
        "new_state": {"type": "string"},
        "created_at": {"type": "number"},
    },
    "required": ["id", "profile_id", "new_state"],
}

BOOT = {
    "type": "object",
    "properties": {
        "profile": PROFILE,
        "base_prompt": {"type": "string"},
        "role_prompt": {"type": "string"},
        "compact_state": {"type": "string"},
        "state_updated_at": NUMBER_OR_NULL,
        "recent_memories": array_of(MEMORY_EVENT),
    },
    "required": ["profile", "base_prompt", "role_prompt", "compact_state"],
}

START_SESSION = {
    "type": "object",
    "properties": {
        "profile": PROFILE,
        "base_prompt": {"type": "string"},
        "role_prompt": {"type": "string"},
        "compact_state": {"type": "string"},
        "identity": STRING_OR_NULL,
        "memories": array_of(HYDRATION_MEMORY),
        "you_got_mail": {"type": "boolean"},
        "server_time": {
            "type": "object",
            "properties": {
                "unix": {"type": "number"},
                "iso": {"type": "string"},
            },
            "required": ["unix", "iso"],
        },
    },
    "required": [
        "profile",
        "base_prompt",
        "role_prompt",
        "compact_state",
        "identity",
        "memories",
        "you_got_mail",
        "server_time",
    ],
}

APPROVAL = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "kind": {"type": "string"},
        "profile_id": STRING_OR_NULL,
        "status": {"type": "string"},
        "payload": JSON_OBJECT,
        "approval_link": {"type": "string"},
    },
    "required": ["id", "kind", "status", "payload"],
}

MESSAGE = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "from_profile_id": {"type": "string"},
        "to_profile_id": {"type": "string"},
        "content": {"type": "string"},
        "created_at": {"type": "number"},
        "read_at": NUMBER_OR_NULL,
    },
    "required": ["id", "from_profile_id", "to_profile_id", "content"],
}

FILE_META = {
    "type": "object",
    "properties": {
        "filename": {"type": "string"},
        "size": {"type": "integer"},
        "updated_at": {"type": "number"},
    },
    "required": ["filename", "size", "updated_at"],
}

FILE_CONTENT = {
    "type": "object",
    "properties": {
        **FILE_META["properties"],
        "content": {"type": "string"},
    },
    "required": ["filename", "size", "updated_at", "content"],
}

DELETED_MEMORY = {
    "type": "object",
    "properties": {
        "deleted": {"type": "boolean"},
        "event_id": {"type": "string"},
    },
    "required": ["deleted", "event_id"],
}

DELETED_FILE = {
    "type": "object",
    "properties": {
        "deleted": {"type": "boolean"},
        "filename": {"type": "string"},
    },
    "required": ["deleted", "filename"],
}

DYNAMIC_FIELD = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["string", "number", "integer", "boolean", "date",
                     "string_list", "object", "object_list"],
        },
        "required": {"type": "boolean"},
    },
    "required": ["type"],
}

DYNAMIC_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "object",
            "additionalProperties": DYNAMIC_FIELD,
        }
    },
    "required": ["fields"],
}

DYNAMIC_STORE = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "profile_id": {"type": "string"},
        "name": {"type": "string"},
        "version": {"type": "integer"},
        "purpose": {"type": "string"},
        "proposed_by": {"type": "string"},
        "schema": DYNAMIC_SCHEMA,
        "status": {
            "type": "string",
            "enum": ["pending", "approved", "rejected", "archived"],
        },
        "rejection_reason": STRING_OR_NULL,
        "created_at": {"type": "number"},
        "updated_at": NUMBER_OR_NULL,
        "approved_at": NUMBER_OR_NULL,
        "rejected_at": NUMBER_OR_NULL,
        "approval_id": {"type": "string"},
        "approval_link": {"type": "string"},
    },
    "required": ["profile_id", "name", "purpose", "schema", "status"],
}

DYNAMIC_RECORD = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "store": {"type": "string"},
        "schema_version": {"type": "integer"},
        "data": JSON_OBJECT,
        "created_at": {"type": "number"},
    },
    "required": ["id", "store", "data"],
}

AUDIT_EVENT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "profile_id": {"type": "string"},
        "store_name": {"type": "string"},
        "action": {"type": "string"},
        "actor": {"type": "string"},
        "detail": {"type": "string"},
        "created_at": {"type": "number"},
    },
    "required": ["id", "profile_id", "store_name", "action", "actor"],
}
