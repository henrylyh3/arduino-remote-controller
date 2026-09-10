from __future__ import annotations

import argparse
from typing import Any

from app import db


def clean_legacy(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    result.pop("_id", None)
    result.pop("_type", None)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Split the legacy single MongoDB collection into domain collections"
    )
    parser.add_argument("--source", default=db.LEGACY_COLLECTION)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--drop-source", action="store_true")
    args = parser.parse_args()

    database = db.database()
    source = database[args.source]
    source_rows = {
        entity: [clean_legacy(row) for row in source.find({"_type": entity}).sort("id", 1)]
        for entity in db.ENTITY_COLLECTIONS
    }
    source_total = sum(len(rows) for rows in source_rows.values())
    if not source_total:
        parser.error(f"No application documents found in legacy collection {args.source!r}")

    db.init_db()
    target_total = sum(db.count(entity) for entity in db.ENTITY_COLLECTIONS)
    if target_total and not args.replace:
        parser.error(
            f"Domain collections already contain {target_total} documents; use --replace to overwrite them"
        )
    if args.replace:
        for entity in db.ENTITY_COLLECTIONS:
            db.delete(entity, {}, many=True)
        db.counters_collection().delete_many({})

    for entity, rows in source_rows.items():
        for row in rows:
            document = dict(row)
            entity_id = int(document.pop("id"))
            db.insert(entity, document, entity_id=entity_id)

    for entity, expected in source_rows.items():
        actual = db.find_all(entity, sort=[("id", 1)])
        if actual != expected:
            raise RuntimeError(f"Verification failed for {entity}")

    if args.drop_source:
        source.drop()

    print(f"Migrated and verified {source_total} documents in database {db.MONGO_DB}")
    for entity, collection_name in db.ENTITY_COLLECTIONS.items():
        print(f"  {collection_name}: {len(source_rows[entity])}")
    print(f"  {db.COUNTERS_COLLECTION}: {db.counters_collection().count_documents({})}")
    print(f"Legacy collection {args.source!r}: {'dropped' if args.drop_source else 'retained'}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
