"""Read-only migration parity gate. Outputs paths/counts/hashes, never values.

Run against frozen BEFORE and migrated-but-not-started AFTER copies:
  python deploy/verify_migration.py before.sqlite3 after.sqlite3 --config-before effective-config.json
The optional config baseline is the old effective PluginConfig.model_dump().
New v3 defaults may be added; every preexisting field must retain its value.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def connect(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('SQLite integrity_check failed')
    return connection


def digest(connection, table, columns):
    query = 'SELECT ' + ','.join(identifier(column) for column in columns) + ' FROM ' + identifier(table)
    rows = [json.dumps(row, ensure_ascii=False, separators=(',', ':'),
                       default=lambda value: {'blob_hex': value.hex()}) for row in connection.execute(query)]
    return len(rows), hashlib.sha256('\n'.join(sorted(rows)).encode()).hexdigest()


def config_differences(before, after, path='config'):
    if isinstance(before, dict):
        if not isinstance(after, dict):
            return [path]
        return [difference for key, value in before.items()
                for difference in config_differences(value, after.get(key), path + '.' + str(key))]
    if isinstance(before, list):
        if not isinstance(after, list) or len(before) != len(after):
            return [path]
        return [difference for index, (left, right) in enumerate(zip(before, after))
                for difference in config_differences(left, right, path + '[' + str(index) + ']')]
    return [] if before == after else [path]


def compare(before_path, after_path, config_before=None):
    before, after = connect(before_path), connect(after_path)
    try:
        result = {'before_schema': before.execute('PRAGMA user_version').fetchone()[0],
                  'after_schema': after.execute('PRAGMA user_version').fetchone()[0], 'tables': {}, 'ok': True}
        tables = [row[0] for row in before.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            columns = [row[1] for row in before.execute('PRAGMA table_info(' + identifier(table) + ')')]
            after_columns = [row[1] for row in after.execute('PRAGMA table_info(' + identifier(table) + ')')]
            if not set(columns).issubset(after_columns):
                result['tables'][table] = {'ok': False, 'reason': 'preexisting columns missing'}
                result['ok'] = False
                continue
            left, right = digest(before, table, columns), digest(after, table, columns)
            result['tables'][table] = {'before_rows': left[0], 'after_rows': right[0],
                                       'before_sha256': left[1], 'after_sha256': right[1], 'ok': left == right}
            result['ok'] = result['ok'] and left == right
        if config_before:
            baseline = json.loads(Path(config_before).read_text(encoding='utf-8-sig'))
            current = after.execute('SELECT r.config_json FROM config_head h JOIN config_revisions r ON r.version=h.version WHERE h.singleton=1').fetchone()
            differences = config_differences(baseline, json.loads(current[0]) if current else None)
            # Field paths may be operational identifiers; do not print values.
            result['config'] = {'ok': not differences, 'differing_paths': differences[:100], 'difference_count': len(differences)}
            result['ok'] = result['ok'] and not differences
        return result
    finally:
        before.close()
        after.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before')
    parser.add_argument('after')
    parser.add_argument('--config-before')
    args = parser.parse_args()
    try:
        result = compare(args.before, args.after, args.config_before)
    except (ValueError, sqlite3.Error, OSError):
        print(json.dumps({'ok': False, 'error': 'Migration parity comparison could not complete; inspect private inputs locally'}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
