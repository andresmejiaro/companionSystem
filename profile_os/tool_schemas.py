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
        "profile_kind": {"type": "string", "enum": ["companion", "system"]},
        "allowed_tools": array_of({"type": "string"}),
        "memory_policy": JSON_OBJECT,
        "closeout_rules": {"type": "string"},
        "aliases": array_of({"type": "string"}),
        "family_id": {"type": "string"},
        "variant_label": {"type": "string"},
        "is_family_default": {"type": "boolean"},
        "created_at": {"type": "number"},
    },
    "required": ["id", "display_name", "description", "signature", "allowed_tools",
                 "memory_policy", "closeout_rules", "aliases", "family_id", "profile_kind",
                 "variant_label", "is_family_default", "created_at"],
}

# Session hydration only carries registry fields that can affect the active
# companion. ``description`` is legacy discovery metadata superseded by the
# canonical ``lane`` prompt section; exposing it here gives the model a second,
# potentially stale statement of scope.
HYDRATION_PROFILE = {
    "type": "object",
    "properties": {
        key: PROFILE["properties"][key]
        for key in (
            "id", "display_name", "signature", "profile_kind", "allowed_tools",
            "memory_policy", "closeout_rules", "aliases", "family_id",
            "variant_label", "is_family_default",
        )
    },
    "required": [
        "id", "display_name", "signature", "profile_kind", "allowed_tools",
        "memory_policy", "closeout_rules", "aliases", "family_id",
        "variant_label", "is_family_default",
    ],
}

BOOT_PROFILE = {
    "type": "object",
    "properties": {
        key: PROFILE["properties"][key]
        for key in PROFILE["properties"]
        if key != "description"
    },
    "required": [key for key in PROFILE["required"] if key != "description"],
}

PROFILE_RESOLUTION = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "status": {"type": "string", "enum": ["resolved", "ambiguous", "not_found"]},
        "match_basis": {
            "type": "string",
            "enum": ["exact_id", "alias", "display_name", "family_default", "none"],
        },
        "resolved_profile_id": STRING_OR_NULL,
        "candidates": array_of(PROFILE),
    },
    "required": [
        "query", "status", "match_basis", "resolved_profile_id", "candidates",
    ],
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

IRONSWORN_MOVE = {
    "type": "object",
    "properties": {
        "move": {"type": "string"},
        "when_it_applies": {"type": "string"},
        "text": {"type": "string"},
        "source": {"type": "string"},
    },
    "required": ["move", "when_it_applies", "text", "source"],
}

IRONSWORN_ORACLE = {
    "type": "object",
    "properties": {
        "oracle": {"type": "string"},
        "use_for": {"type": "string"},
        "text": {"type": "string"},
        "source": {"type": "string"},
    },
    "required": ["oracle", "use_for", "text", "source"],
}

IRONSWORN_SHEET = {
    "type": "object",
    "properties": {
        "filename": {"type": "string"},
        "updated_at": {"type": "number"},
        "sheet": {"type": "object"},
    },
    "required": ["filename", "updated_at", "sheet"],
}

IRONSWORN_RESOURCE = {
    "type": "object",
    "properties": {
        "resource": {"type": "string", "enum": ["move", "oracle", "sheet"]},
        "item": {
            "oneOf": [IRONSWORN_MOVE, IRONSWORN_ORACLE, IRONSWORN_SHEET],
        },
    },
    "required": ["resource", "item"],
}

IRONSWORN_DICE = {
    "type": "object",
    "properties": {
        "action_die": {"type": "integer", "minimum": 1, "maximum": 6},
        "challenge_dice": {
            "type": "array", "minItems": 2, "maxItems": 2,
            "items": {"type": "integer", "minimum": 1, "maximum": 10},
        },
    },
    "required": ["action_die", "challenge_dice"],
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
        "profile": BOOT_PROFILE,
        "who_you_are": {"type": "string"},
        "signature": {"type": "string"},
        "lane": {"type": "string"},
        "voice": {"type": "string"},
        "what_you_do": {"type": "string"},
        "how_you_keep_context": {"type": "string"},
        "compact_state": {"type": "string"},
        "state_updated_at": NUMBER_OR_NULL,
        "recent_memories": array_of(MEMORY_EVENT),
    },
    "required": ["profile", "who_you_are", "signature", "lane", "voice",
                 "what_you_do", "how_you_keep_context", "compact_state",
                 "state_updated_at", "recent_memories"],
}

START_SESSION = {
    "type": "object",
    "properties": {
        "profile": HYDRATION_PROFILE,
        "who_you_are": {"type": "string"},
        "signature": {"type": "string"},
        "lane": {"type": "string"},
        "voice": {"type": "string"},
        "what_you_do": {"type": "string"},
        "how_you_keep_context": {"type": "string"},
        "compact_state": {"type": "string"},
        "system_contracts": {
            "type": "object",
            "properties": {
                "companion": {"type": "string"},
            },
        },
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
        "selection": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string"},
                "family_id": {"type": "string"},
                "variant_label": {"type": "string"},
                "settled": {"type": "boolean"},
            },
            "required": ["profile_id", "family_id", "variant_label", "settled"],
        },
        "routing_guidance": {"type": "string"},
        "companion_directory": array_of(JSON_OBJECT),
        "data_sources": {
            "type": "object",
            "properties": {
                "profile_stores": {"type": "array", "items": JSON_OBJECT},
                "joined_projects": {"type": "array", "items": JSON_OBJECT},
            },
            "required": ["profile_stores", "joined_projects"],
        },
        "server_time": {
            "type": "object",
            "properties": {
                "unix": {"type": "number"},
                "iso": {"type": "string"},
                "madrid_iso": {"type": "string"},
            },
            "required": ["unix", "iso", "madrid_iso"],
        },
        # Present only for the tool_probe companion. The MCP adapter owns the
        # runtime registry and attaches this diagnostic snapshot.
        "server_tool_catalog": JSON_OBJECT,
        # Present only for summon_companion(mode="forum"). Exact posts and causal windows
        # are joined by The Thread; this is the companion-owned durable slice.
        "thread_continuity": {"type": "array", "items": JSON_OBJECT},
        "thread_continuity_write_contract": JSON_OBJECT,
    },
    "required": [
        "profile",
        "who_you_are",
        "signature",
        "lane",
        "voice",
        "what_you_do",
        "how_you_keep_context",
        "compact_state",
        "system_contracts",
        "identity",
        "memories",
        "recent_exchanges",
        "you_got_mail",
        "selection",
        "routing_guidance",
        "companion_directory",
        "data_sources",
        "server_time",
    ],
}

CONTEXT_RESULT = {
    "type": "object",
    "properties": {
        "source_type": {
            "type": "string",
            "enum": ["memory", "profile_store", "shared_project"],
        },
        "source_name": {"type": "string"},
        "project_id": {"type": "string"},
        "item": JSON_OBJECT,
    },
    "required": ["source_type", "source_name", "item"],
}

PREPARE_CLOSEOUT = {
    "type": "object",
    "properties": {
        "profile_id": {"type": "string"},
        "instructions": array_of({"type": "string"}),
    },
    "required": ["profile_id", "instructions"],
    "additionalProperties": False,
}

# ``closeout`` is deliberately a two-call MCP tool.  The first call prepares
# the handoff and returns a one-time code; the second persists the closeout.
# This flattened union avoids ``oneOf`` because some connector validators do
# not support it reliably.
MCP_CLOSEOUT = {
    "type": "object",
    "properties": {
        "phase": {"type": "string", "enum": ["prepared", "closed"]},
        "profile_id": {"type": "string"},
        "code": {"type": "string"},
        "expires_at": {"type": "number"},
        "instructions": array_of({"type": "string"}),
        "closeout": CLOSEOUT,
    },
    "required": ["phase"],
    "additionalProperties": False,
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
                "who_you_are": STRING_OR_NULL,
                "signature": STRING_OR_NULL,
                "lane": STRING_OR_NULL,
                "voice": STRING_OR_NULL,
                "what_you_do": STRING_OR_NULL,
                "how_you_keep_context": STRING_OR_NULL,
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

QUESTION_OPTION = {
    "type": "object",
    "properties": {"label": {"type": "string"}, "text": {"type": "string"}},
    "required": ["label", "text"],
}

QUESTION_DRAW_ITEM = {
    "type": "object",
    "properties": {
        "position": {"type": "integer"},
        "question_ref": {"type": "string"},
        "domain": {"type": "string"},
        "prompt": {"type": "string"},
        "options": array_of(QUESTION_OPTION),
    },
    "required": ["position", "question_ref", "domain", "prompt", "options"],
}

QUESTION_DRAW = {
    "type": "object",
    "properties": {
        "attempt_code": {"type": "string"},
        "expires_at": {"type": "number"},
        "markdown": {"type": "string"},
        "questions": array_of(QUESTION_DRAW_ITEM),
    },
    "required": ["attempt_code", "expires_at", "markdown", "questions"],
}

QUESTION_GRADE_RESULT = {
    "type": "object",
    "properties": {
        "position": {"type": "integer"},
        "status": {"type": "string", "enum": ["correct", "wrong", "nullified"]},
        "markdown": {"type": "string"},
    },
    "required": ["position", "status", "markdown"],
}

QUESTION_GRADE = {
    "type": "object",
    "properties": {
        "attempt_code": {"type": "string"},
        "markdown": {"type": "string"},
        "results": array_of(QUESTION_GRADE_RESULT),
    },
    "required": ["attempt_code", "markdown", "results"],
}

QUESTION_WEAKNESS = {
    "type": "object",
    "properties": {
        "domain": {"type": "string"},
        "sub_skill": STRING_OR_NULL,
        "wrong_count": {"type": "integer"},
        "times_shown": {"type": "integer"},
        "question_count": {"type": "integer"},
    },
    "required": ["domain", "sub_skill", "wrong_count", "times_shown", "question_count"],
}

QUESTION_WEAKNESS_REPORT = {
    "type": "object",
    "properties": {"items": array_of(QUESTION_WEAKNESS)},
    "required": ["items"],
}

QUESTION_REVISION = {
    "type": "object",
    "properties": {
        "question_ref": {"type": "string"},
        "action": {"type": "string"},
        "answer_status": {"type": "string"},
        "weight": {"type": "number"},
        "correct_count": {"type": "integer"},
        "wrong_count": {"type": "integer"},
    },
    "required": ["question_ref", "action", "answer_status", "weight",
                 "correct_count", "wrong_count"],
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
