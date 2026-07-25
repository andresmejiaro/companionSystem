"""Hash-only verification for the base/role prompt-file rename.

This module deliberately reads prompt files as bytes and reports only SHA-256
hashes. It is intended for the production backup/migration check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .storage import PROMPT_SECTION_FILES


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(data_dir: Path) -> dict:
    profiles = data_dir / "profiles"
    return {
        profile_dir.name: {
            "base_prompt": _hash(profile_dir / "base_prompt.md"),
            "role_prompt": _hash(profile_dir / "role_prompt.md"),
        }
        for profile_dir in sorted(profiles.iterdir())
        if profile_dir.is_dir()
    }


def verify(data_dir: Path, expected: dict) -> dict:
    profiles = data_dir / "profiles"
    result = {}
    for profile_id, hashes in expected.items():
        profile_dir = profiles / profile_id
        who = profile_dir / PROMPT_SECTION_FILES["who_you_are"]
        what = profile_dir / PROMPT_SECTION_FILES["what_you_do"]
        empty = {
            name: (profile_dir / filename).read_bytes() == b""
            for name, filename in PROMPT_SECTION_FILES.items()
            if name not in {"who_you_are", "what_you_do"}
        }
        result[profile_id] = {
            "who_you_are_matches_base_prompt": who.exists() and _hash(who) == hashes["base_prompt"],
            "what_you_do_matches_role_prompt": what.exists() and _hash(what) == hashes["role_prompt"],
            "empty_fields": empty,
            "who_you_are_sha256": _hash(who) if who.exists() else None,
            "what_you_do_sha256": _hash(what) if what.exists() else None,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("snapshot", "verify"))
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "snapshot":
        args.manifest.write_text(json.dumps(snapshot(args.data_dir), indent=2, sort_keys=True) + "\n")
        return 0
    expected = json.loads(args.manifest.read_text())
    result = verify(args.data_dir, expected)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if all(
        item["who_you_are_matches_base_prompt"]
        and item["what_you_do_matches_role_prompt"]
        and all(item["empty_fields"].values())
        for item in result.values()
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
