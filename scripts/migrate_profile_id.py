"""Identity-preserving rename of an existing Profile OS companion."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def migrate(data_dir: Path, old_id: str, new_id: str, display_name: str,
            aliases: list[str], store_renames: dict[str, str]) -> None:
    db_path = data_dir / "profile_os.db"
    old_dir = data_dir / "profiles" / old_id
    new_dir = data_dir / "profiles" / new_id
    if not db_path.is_file() or not old_dir.is_dir():
        raise SystemExit(f"missing database or profile directory for {old_id!r}")
    if new_dir.exists():
        raise SystemExit(f"target profile directory already exists: {new_dir}")

    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys = OFF")
    row = db.execute(
        "SELECT display_name, aliases FROM profiles WHERE id=?", (old_id,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"profile does not exist: {old_id}")
    if db.execute("SELECT 1 FROM profiles WHERE id=?", (new_id,)).fetchone():
        raise SystemExit(f"target profile already exists: {new_id}")

    merged_aliases = list(json.loads(row[1]))
    for alias in [old_id, row[0], *aliases]:
        if alias and alias.casefold() not in {item.casefold() for item in merged_aliases}:
            merged_aliases.append(alias)

    moved = False
    try:
        db.execute("BEGIN IMMEDIATE")
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        for table in tables:
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            for column in columns:
                if column == "profile_id" or column.endswith("_profile_id"):
                    db.execute(
                        f'UPDATE "{table}" SET "{column}"=? WHERE "{column}"=?',
                        (new_id, old_id),
                    )

        db.execute(
            "UPDATE profiles SET id=?, display_name=?, aliases=? WHERE id=?",
            (new_id, display_name, json.dumps(merged_aliases), old_id),
        )
        for old_store, new_store in store_renames.items():
            collision = db.execute(
                "SELECT 1 FROM dynamic_stores WHERE profile_id=? AND name=?",
                (new_id, new_store),
            ).fetchone()
            if collision:
                raise RuntimeError(f"target store already exists: {new_store}")
            db.execute(
                "UPDATE dynamic_stores SET name=? WHERE profile_id=? AND name=?",
                (new_store, new_id, old_store),
            )
            db.execute(
                "UPDATE dynamic_records SET store_name=? WHERE profile_id=? AND store_name=?",
                (new_store, new_id, old_store),
            )
            db.execute(
                "UPDATE store_audit SET store_name=? WHERE profile_id=? AND store_name=?",
                (new_store, new_id, old_store),
            )

        old_dir.replace(new_dir)
        moved = True
        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign-key violations after migration: {violations!r}")
        db.commit()
    except Exception:
        db.rollback()
        if moved and not old_dir.exists():
            new_dir.replace(old_dir)
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("old_id")
    parser.add_argument("new_id")
    parser.add_argument("display_name")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--rename-store", action="append", default=[])
    args = parser.parse_args()
    store_renames = dict(item.split("=", 1) for item in args.rename_store)
    migrate(args.data_dir, args.old_id, args.new_id, args.display_name,
            args.alias, store_renames)


if __name__ == "__main__":
    main()
