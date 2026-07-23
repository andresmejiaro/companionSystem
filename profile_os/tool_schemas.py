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
        "signature": {"type": "string", "maxLength": 5},
        "allowed_tools": array_of({"type": "string"}),
        "memory_policy": JSON_OBJECT,
        "closeout_rules": {"type": "string"},
        "created_at": {"type": "number"},
    },
    "required": ["id", "display_name", "description", "signature", "allowed_tools",
                 "memory_policy", "closeout_rules", "created_at"],
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
    "required": ["id", "profile_id", "notes", "facts", "texture", "exchange",
                 "new_state", "created_at"],
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
    "required": ["profile", "base_prompt", "role_prompt", "compact_state",
                 "state_updated_at", "recent_memories"],
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
        "recent_exchanges": array_of({
            "type": "object",
            "properties": {
                "texture": {"type": "string"},
                "exchange": {"type": "string"},
            },
            "required": ["texture", "exchange"],
        }),
        "you_got_mail": {"type": "boolean"},
        "routing_guidance": {"type": "string"},
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
        "recent_exchanges",
        "you_got_mail",
        "routing_guidance",
        "server_time",
    ],
}

APPROVAL = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "kind": {"type": "string", "enum": ["prompt_edit", "store_schema",
                                                    "project_create", "project_join"]},
        "profile_id": STRING_OR_NULL,
        "status": {"type": "string"},
        # Deliberately flattened: some MCP connector validators reject oneOf.
        "payload": {
            "type": "object",
            "properties": {
                "base_prompt": STRING_OR_NULL,
                "role_prompt": STRING_OR_NULL,
                "store_id": {"type": "string"},
                "store_name": {"type": "string"},
                "project_id": {"type": "string"},
                "project_name": {"type": "string"},
                "joining_profile_id": {"type": "string"},
                "name": {"type": "string"},
                "purpose": {"type": "string"},
                "schema": JSON_OBJECT,
            },
            "required": [],
            "additionalProperties": False,
        },
        "approval_link": {"type": "string"},
    },
    "required": ["id", "kind", "profile_id", "status", "payload"],
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
        "approved_at": NUMBER_OR_NULL,
        "rejected_at": NUMBER_OR_NULL,
        "approval_id": {"type": "string"},
        "approval_link": {"type": "string"},
    },
    "required": ["id", "profile_id", "name", "version", "purpose",
                 "proposed_by", "schema", "status", "rejection_reason",
                 "created_at", "approved_at", "rejected_at"],
}

DYNAMIC_RECORD = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "store": {"type": "string"},
        "schema_version": {"type": "integer"},
        "data": JSON_OBJECT,
        "created_at": {"type": "number"},
        "updated_at": NUMBER_OR_NULL,
    },
    "required": ["id", "store", "schema_version", "data", "created_at", "updated_at"],
}

PROJECT_MEMBER = {
    "type": "object",
    "properties": {"profile_id": {"type": "string"}, "role": {"type": "string"},
                   "joined_at": {"type": "number"}},
    "required": ["profile_id", "role", "joined_at"],
}

PROJECT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"}, "name": {"type": "string"},
        "purpose": {"type": "string"}, "schema": DYNAMIC_SCHEMA,
        "created_by_profile_id": {"type": "string"},
        "status": {"type": "string", "enum": ["pending", "active"]},
        "created_at": {"type": "number"}, "approved_at": NUMBER_OR_NULL,
        "members": array_of(PROJECT_MEMBER), "viewer_is_member": {"type": "boolean"},
        "approval_id": {"type": "string"}, "approval_link": {"type": "string"},
    },
    "required": ["id", "name", "purpose", "schema", "created_by_profile_id",
                 "status", "created_at", "approved_at", "members"],
}

PROJECT_WITH_APPROVAL = {
    **PROJECT,
    "required": [*PROJECT["required"], "approval_id"],
}

PROJECT_RECORD = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "project_id": {"type": "string"},
                   "data": JSON_OBJECT, "created_by_profile_id": {"type": "string"},
                   "created_at": {"type": "number"}},
    "required": ["id", "project_id", "data", "created_by_profile_id", "created_at"],
}

DELETED_RECORD = {
    "type": "object",
    "properties": {"deleted": {"type": "boolean"}, "record_id": {"type": "string"},
                   "store": {"type": "string"}},
    "required": ["deleted", "record_id", "store"],
}

LEFT_PROJECT = {
    "type": "object",
    "properties": {"left": {"type": "boolean"}, "project_id": {"type": "string"},
                   "empty": {"type": "boolean"}},
    "required": ["left", "project_id", "empty"],
}

# Reused by MCP input and output schemas.  Keeping definitions centralized
# makes references stable without relying on discriminator/oneOf support.
SHARED_DEFS = {
    "Profile": PROFILE, "Project": PROJECT, "ProjectRecord": PROJECT_RECORD,
    "DynamicRecord": DYNAMIC_RECORD, "DynamicSchema": DYNAMIC_SCHEMA,
    "ListEnvelope": {"type": "object", "properties": {"items": {"type": "array"}},
                     "required": ["items"]},
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
