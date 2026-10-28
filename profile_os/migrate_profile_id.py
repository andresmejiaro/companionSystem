"""Offline canonical profile-ID migration. Stop writers and back up data first."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .storage import Store

ID_KEYS = {'profile_id', 'from_profile_id', 'to_profile_id', 'owner_profile_id',
           'created_by_profile_id', 'joining_profile_id', 'performer_profile_id',
           'source_companion'}


def _structured_ids(value, old, new):
    if isinstance(value, dict):
        return {k: (new if k in ID_KEYS and v == old else _structured_ids(v, old, new))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_structured_ids(v, old, new) for v in value]
    return value


def _hashes(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


def migrate(data_dir: str | Path, old: str, new: str) -> dict:
    """Preserve record IDs/content and compensate a failed filesystem move.

    SQLite updates are one deferred-FK transaction. The directory is renamed
    before commit, and moved back on failure. A process/host crash in that tiny
    interval requires recovery from the mandatory offline backup.
    """
    if old == new or not all(re.fullmatch(r'[A-Za-z0-9_-]+', x) for x in (old, new)):
        raise ValueError('distinct valid profile IDs required')
    root = Path(data_dir)
    source, target = root / 'profiles' / old, root / 'profiles' / new
    if not source.is_dir() or source.is_symlink() or target.exists():
        raise ValueError('source directory missing/unsafe or target directory exists')
    before_files = _hashes(source)
    conn = sqlite3.connect(root / 'profile_os.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    moved = False
    changed = {}
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('PRAGMA defer_foreign_keys=ON')
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('pre-existing foreign-key violations')
        profile = conn.execute('SELECT * FROM profiles WHERE id=?', (old,)).fetchone()
        if profile is None or conn.execute('SELECT 1 FROM profiles WHERE id=?', (new,)).fetchone():
            raise ValueError('source profile missing or target profile exists')
        aliases = json.loads(profile['aliases'])
        if old not in aliases:
            aliases.append(old)
        # Source and target must not conflict with another profile routing alias.
        for row in conn.execute('SELECT id,aliases FROM profiles WHERE id<>?', (old,)):
            if new in json.loads(row['aliases']) or old in json.loads(row['aliases']):
                raise ValueError('routing alias collision')
        conn.execute('CREATE TABLE IF NOT EXISTS profile_id_redirects ('
                     'retired_id TEXT PRIMARY KEY, target_id TEXT NOT NULL '
                     'REFERENCES profiles(id) ON DELETE CASCADE)')
        if conn.execute('SELECT 1 FROM profile_id_redirects WHERE retired_id=?', (new,)).fetchone():
            raise ValueError('target is a reserved retired ID')
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            # Only schema identifiers from SQLite are interpolated.
            quoted = '"' + table.replace('"', '""') + '"'
            columns = conn.execute(f'PRAGMA table_info({quoted})').fetchall()
            for col in columns:
                name = col['name']
                qcol = '"' + name.replace('"', '""') + '"'
                if name in ID_KEYS or (table == 'profiles' and name == 'id') or (
                    table == 'profile_id_redirects' and name == 'target_id') or (
                    table == 'store_audit' and name == 'actor'):
                    n = conn.execute(f'UPDATE {quoted} SET {qcol}=? WHERE {qcol}=?', (new, old)).rowcount
                    if n:
                        changed[f'{table}.{name}'] = n
                # Update structured routing fields, preserving narrative strings,
                # memory tags, historic audit detail and arbitrary business data.
                if name in {'payload', 'data'} and col['type'].upper() == 'TEXT':
                    for row in conn.execute(f'SELECT rowid,{qcol} FROM {quoted} WHERE {qcol} LIKE ?', ('%'+old+'%',)).fetchall():
                        try:
                            data = json.loads(row[1])
                        except (ValueError, TypeError):
                            continue
                        updated = _structured_ids(data, old, new)
                        if updated != data:
                            conn.execute(f'UPDATE {quoted} SET {qcol}=? WHERE rowid=?',
                                         (json.dumps(updated, ensure_ascii=False), row[0]))
                            key = f'{table}.{name}.structured_ids'
                            changed[key] = changed.get(key, 0) + 1
        conn.execute('UPDATE profiles SET aliases=? WHERE id=?', (json.dumps(aliases), new))
        conn.execute('INSERT INTO profile_id_redirects VALUES (?,?)', (old, new))
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('foreign-key verification failed')
        source.rename(target)
        moved = True
        if _hashes(target) != before_files:
            raise ValueError('profile file hash verification failed')
        conn.commit()
        return {'old': old, 'new': new, 'updated': changed,
                'files_preserved': len(before_files), 'foreign_keys_ok': True}
    except BaseException:
        conn.rollback()
        if moved:
            target.rename(source)
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--old', required=True)
    parser.add_argument('--new', required=True)
    parser.add_argument('--apply', action='store_true', required=True,
                        help='writers must be stopped and data backed up')
    args = parser.parse_args()
    print(json.dumps(migrate(args.data_dir, args.old, args.new), indent=2))


if __name__ == '__main__':
    main()
