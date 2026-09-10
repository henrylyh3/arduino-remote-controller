from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from app import db


TABLES = (
    "nodes",
    "devices",
    "buttons",
    "timers",
    "schedules",
    "workflows",
    "timer_presets",
    "workflow_steps",
    "workflow_schedules",
    "workflow_runs",
    "workflow_run_steps",
    "ac_controllers",
    "events",
)


def read_rows(source: Path, table: str) -> list[dict[str, Any]]:
    with sqlite3.connect(source) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')]


def main() -> int:
    parser = argparse.ArgumentParser(description="One-time SQLite to MongoDB migration")
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent / "smart_home.sqlite3",
    )
    parser.add_argument("--replace", action="store_true", help="Replace existing collection contents")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if not source.is_file():
        parser.error(f"SQLite database not found: {source}")

    db.init_db()
    existing = sum(db.count(table) for table in TABLES)
    if existing and not args.replace:
        parser.error(
            f"MongoDB already has {existing} application documents; rerun with --replace to overwrite them"
        )
    if args.replace:
        for table in TABLES:
            db.delete(table, {}, many=True)
        db.counters_collection().delete_many({})

    totals: dict[str, int] = {}
    for table in TABLES:
        rows = read_rows(source, table)
        for row in rows:
            entity_id = int(row.pop("id"))
            db.insert(table, row, entity_id=entity_id)
        totals[table] = len(rows)

    migrated = sum(totals.values())
    print(f"Migrated {migrated} records from {source}")
    for table, count in totals.items():
        print(f"  {table}: {count}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
