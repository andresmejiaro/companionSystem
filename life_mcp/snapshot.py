from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
CATEGORIES = {"job", "education", "certification", "coursera", "ecole42", "project", "other"}
PRIVATE_KEYS = {"source_rel_path", "evidence", "docs_root"}


class SnapshotError(ValueError):
    pass


class SnapshotNotFound(LookupError):
    pass


class SnapshotReadOnly(SnapshotError):
    pass


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _private_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PRIVATE_KEYS:
                return key
            if found := _private_key(item):
                return found
    elif isinstance(value, list):
        for item in value:
            if found := _private_key(item):
                return found
    return None


class SnapshotStore:
    """Versioned, atomically published read model for career facts."""

    def __init__(self, data_dir: Path, *, read_only: bool = False):
        self.data_dir = data_dir
        self.read_only = read_only
        self.snapshots_dir = data_dir / "snapshots"
        self.current_path = data_dir / "current.json"
        self.lock = threading.RLock()
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def prepare(raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise SnapshotError("snapshot must be a JSON object")
        summary = copy.deepcopy(raw.get("summary"))
        entities = copy.deepcopy(raw.get("entities"))
        if not isinstance(summary, dict) or not isinstance(entities, list):
            raise SnapshotError("snapshot requires object summary and array entities")
        # Local filesystem topology is not part of the companion-safe projection.
        summary.pop("docs_root", None)
        keys: set[str] = set()
        for position, entity in enumerate(entities):
            if not isinstance(entity, dict):
                raise SnapshotError(f"entity {position} must be an object")
            key = entity.get("entity_key")
            if not isinstance(key, str) or not key.strip():
                raise SnapshotError(f"entity {position} requires entity_key")
            if key in keys:
                raise SnapshotError(f"duplicate entity_key: {key}")
            keys.add(key)
            category = entity.get("category")
            if category not in CATEGORIES:
                raise SnapshotError(f"invalid category for {key}: {category!r}")
            if found := _private_key(entity):
                raise SnapshotError(f"private field {found!r} present in {key}")
        prepared = {"summary": summary, "entities": entities}
        if len(_canonical_bytes(prepared)) > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("snapshot exceeds maximum size")
        return prepared

    def publish(self, raw: dict[str, Any]) -> dict[str, Any]:
        if self.read_only:
            raise SnapshotReadOnly(
                "Life MCP is in read-only mode; snapshot publication is disabled")
        prepared = self.prepare(raw)
        with self.lock:
            previous = self.load(required=False)
            previous_id = (
                previous.get("publication", {}).get("snapshot_id") if previous else None
            )
            source_generated_at = prepared["summary"].get("generated_at_utc")
            digest_input = {
                "source_generated_at": source_generated_at,
                "summary": prepared["summary"],
                "entities": prepared["entities"],
            }
            snapshot_id = hashlib.sha256(_canonical_bytes(digest_input)).hexdigest()
            if (
                previous
                and previous.get("publication", {}).get("snapshot_id") == snapshot_id
            ):
                return previous["publication"]
            publication = {
                "schema_version": 1,
                "snapshot_id": snapshot_id,
                "previous_snapshot_id": previous_id,
                "published_at": _utc_now(),
                "source_generated_at": source_generated_at,
                "entity_count": len(prepared["entities"]),
            }
            payload = {"publication": publication, **prepared}
            encoded = _canonical_bytes(payload)
            version_path = self.snapshots_dir / f"{snapshot_id}.json"
            if not version_path.exists():
                _atomic_write(version_path, encoded)
            _atomic_write(self.current_path, encoded)
            return publication

    def load(self, *, required: bool = True) -> dict[str, Any] | None:
        with self.lock:
            if not self.current_path.exists():
                if required:
                    raise SnapshotNotFound("no career truth snapshot has been published")
                return None
            try:
                value = json.loads(self.current_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SnapshotError("published snapshot is unreadable") from exc
            if not isinstance(value, dict):
                raise SnapshotError("published snapshot is malformed")
            return value

    def status(self) -> dict[str, Any]:
        snapshot = self.load(required=False)
        if snapshot is None:
            return {"published": False}
        summary = snapshot["summary"]
        return {
            "published": True,
            **snapshot["publication"],
            "categories": summary.get("categories", {}),
            "conflicts_count": summary.get("conflicts_count"),
            "resolution_policy": summary.get("resolution_policy"),
        }

    def get(self, entity_key: str) -> dict[str, Any]:
        snapshot = self.load()
        assert snapshot is not None
        for entity in snapshot["entities"]:
            if entity["entity_key"] == entity_key:
                return {
                    "snapshot_id": snapshot["publication"]["snapshot_id"],
                    "source_generated_at": snapshot["publication"]["source_generated_at"],
                    "entity": entity,
                }
        raise SnapshotNotFound(f"unknown career entity: {entity_key}")

    def search(self, query: str, category: str | None, limit: int) -> dict[str, Any]:
        snapshot = self.load()
        assert snapshot is not None
        needle = query.casefold().strip()
        matches = []
        for entity in snapshot["entities"]:
            if category and entity.get("category") != category:
                continue
            haystack = json.dumps(entity, ensure_ascii=False).casefold()
            if needle in haystack:
                matches.append(self._compact(entity))
            if len(matches) >= limit:
                break
        return self._result_envelope(snapshot, matches)

    def timeline(self, category: str | None, limit: int, offset: int) -> dict[str, Any]:
        snapshot = self.load()
        assert snapshot is not None
        entities = [
            entity for entity in snapshot["entities"]
            if not category or entity.get("category") == category
        ]
        entities.sort(key=lambda item: (
            item.get("start_date") or "9999-99-99", item.get("entity_key", "")
        ))
        items = [self._compact(item) for item in entities[offset:offset + limit]]
        result = self._result_envelope(snapshot, items)
        result.update({"total": len(entities), "offset": offset, "limit": limit})
        return result

    def provenance(self, entity_key: str) -> dict[str, Any]:
        found = self.get(entity_key)
        entity = found["entity"]
        return {
            "snapshot_id": found["snapshot_id"],
            "source_generated_at": found["source_generated_at"],
            "entity_key": entity_key,
            "sources": entity.get("sources", []),
            "source_types": entity.get("source_types", []),
            "resolution_policy": entity.get("resolution_policy"),
            "source_precedence_note": entity.get("source_precedence_note"),
            "tags": entity.get("tags", []),
        }

    @staticmethod
    def _compact(entity: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "entity_key", "category", "organization", "title", "start_date",
            "start_precision", "end_date", "end_precision", "highlights",
            "role_description", "responsibilities", "subjects_taught",
            "technologies", "achievements", "tags",
        )
        return {key: entity[key] for key in fields if key in entity}

    @staticmethod
    def _result_envelope(snapshot: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
        publication = snapshot["publication"]
        return {
            "snapshot_id": publication["snapshot_id"],
            "source_generated_at": publication["source_generated_at"],
            "items": items,
        }
