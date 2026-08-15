"""Validate and publish one generated life snapshot.

There is deliberately no HTTP equivalent. Production publication happens by
running this module through the SSH-controlled container stdin.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .snapshot import SnapshotStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--stdin", action="store_true", dest="from_stdin")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.from_stdin == bool(args.path):
        raise SystemExit("provide exactly one snapshot path or --stdin")
    text = sys.stdin.read() if args.from_stdin else args.path.read_text(encoding="utf-8")
    raw = json.loads(text)
    prepared = SnapshotStore.prepare(raw)
    if args.validate_only:
        print(json.dumps({"valid": True, "entity_count": len(prepared["entities"])}))
        return
    settings = Settings.from_env()
    data_dir = args.data_dir or settings.data_dir
    publication = SnapshotStore(data_dir, read_only=settings.read_only).publish(prepared)
    print(json.dumps(publication, sort_keys=True))


if __name__ == "__main__":
    main()
