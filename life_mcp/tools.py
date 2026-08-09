from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .snapshot import CATEGORIES, SnapshotStore


Handler = Callable[[dict[str, Any]], Any]


def obj(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler

    def advertised(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }


STR = {"type": "string"}
CATEGORY = {"type": "string", "enum": sorted(CATEGORIES)}
LIMIT_50 = {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}


def build_tools(store: SnapshotStore) -> dict[str, Tool]:
    tools = [
        Tool(
            "career_source_status",
            "Report the currently published career-truth snapshot, its source generation time, entity counts, conflicts, and resolution policy. Call this before treating time-sensitive career facts as current.",
            obj(),
            lambda _: store.status(),
        ),
        Tool(
            "search_career_entities",
            "Search the published career truth across jobs, education, certifications, Coursera, 42, and projects. Returns compact resolved entities from one immutable snapshot.",
            obj({"query": {"type": "string", "minLength": 1},
                 "category": CATEGORY, "limit": LIMIT_50}, ["query"]),
            lambda args: store.search(
                args["query"], args.get("category"), int(args.get("limit", 20))
            ),
        ),
        Tool(
            "get_career_entity",
            "Get one complete resolved career entity by its stable entity_key from the published snapshot.",
            obj({"entity_key": STR}, ["entity_key"]),
            lambda args: store.get(args["entity_key"]),
        ),
        Tool(
            "list_career_timeline",
            "List the published career timeline in chronological order, optionally restricted to one category.",
            obj({
                "category": CATEGORY,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            }),
            lambda args: store.timeline(
                args.get("category"), int(args.get("limit", 100)),
                int(args.get("offset", 0)),
            ),
        ),
        Tool(
            "get_career_provenance",
            "Get source hashes, source types, tags, and resolution policy for one career entity. Raw documents, local paths, and evidence excerpts are never returned.",
            obj({"entity_key": STR}, ["entity_key"]),
            lambda args: store.provenance(args["entity_key"]),
        ),
    ]
    return {tool.name: tool for tool in tools}
