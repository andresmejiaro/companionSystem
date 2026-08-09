from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar


T = TypeVar("T")


class JsonState:
    """Small atomic JSON store for OAuth clients and one-use codes."""

    def __init__(self, path: Path, default: dict[str, Any] | None = None):
        self.path = path
        self.default = default or {}
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, Any]:
        with self.lock:
            if not self.path.exists():
                return json.loads(json.dumps(self.default))
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return json.loads(json.dumps(self.default))

    def write(self, data: dict[str, Any]) -> None:
        with self.lock:
            fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, self.path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)

    def mutate(self, fn: Callable[[dict[str, Any]], T]) -> T:
        with self.lock:
            data = self.read()
            result = fn(data)
            self.write(data)
            return result
