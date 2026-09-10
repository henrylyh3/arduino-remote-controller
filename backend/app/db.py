from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError


def _load_local_env() -> None:
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


MONGO_URI = os.environ.get("MONGO_URI", "").strip()
MONGO_DB = os.environ.get("MONGO_DB", "smart_controller").strip()
LEGACY_COLLECTION = os.environ.get(
    "MONGO_LEGACY_COLLECTION", os.environ.get("MONGO_COLLECTION", "esp32")
).strip()

ENTITY_COLLECTIONS = {
    "nodes": "nodes",
    "devices": "devices",
    "buttons": "signals",
    "ac_controllers": "ac_controllers",
    "timers": "timers",
    "timer_presets": "timer_presets",
    "schedules": "schedules",
    "workflows": "workflows",
    "workflow_steps": "workflow_steps",
    "workflow_schedules": "workflow_schedules",
    "workflow_runs": "workflow_runs",
    "workflow_run_steps": "workflow_run_steps",
    "events": "events",
}
COUNTERS_COLLECTION = "_counters"

_client: MongoClient[dict[str, Any]] | None = None
_database: Database[dict[str, Any]] | None = None


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def database() -> Database[dict[str, Any]]:
    global _client, _database
    if _database is not None:
        return _database
    if not MONGO_URI:
        raise RuntimeError(
            "MONGO_URI is required. Set it in the environment or the application config.env file."
        )
    _client = MongoClient(
        MONGO_URI,
        appname="smart-home-controller",
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        socketTimeoutMS=12000,
    )
    _database = _client[MONGO_DB]
    return _database


def collection(entity: str) -> Collection[dict[str, Any]]:
    try:
        name = ENTITY_COLLECTIONS[entity]
    except KeyError as exc:
        raise ValueError(f"Unknown MongoDB entity: {entity}") from exc
    return database()[name]


def counters_collection() -> Collection[dict[str, Any]]:
    return database()[COUNTERS_COLLECTION]


def init_db() -> None:
    database()
    assert _client is not None
    _client.admin.command("ping")

    for name in sorted(set(ENTITY_COLLECTIONS.values())):
        coll = database()[name]
        coll.create_index([("id", ASCENDING)], unique=True, name="id_unique")
        coll.create_index(
            [("created_at", DESCENDING), ("id", DESCENDING)],
            name="created_desc",
        )

    collection("nodes").create_index([("base_url", ASCENDING)], unique=True, name="base_url_unique")
    collection("devices").create_index([("node_id", ASCENDING), ("name", ASCENDING)], name="node_name")
    collection("buttons").create_index(
        [("device_id", ASCENDING), ("signal_type", ASCENDING)], name="device_signal_type"
    )
    controller_collection = collection("ac_controllers")
    if "node_id_unique" in controller_collection.index_information():
        controller_collection.drop_index("node_id_unique")
    controller_collection.update_many(
        {"last_node_id": {"$exists": False}, "node_id": {"$exists": True}},
        [{"$set": {"last_node_id": "$node_id"}}],
    )
    controller_collection.update_many({}, {"$unset": {"node_id": ""}})
    controller_collection.create_index(
        [("last_node_id", ASCENDING)], name="last_node_id"
    )
    collection("timers").create_index(
        [("status", ASCENDING), ("run_at_utc", ASCENDING)], name="due"
    )
    collection("timer_presets").create_index(
        [("active", ASCENDING), ("run_at_utc", ASCENDING)], name="due"
    )
    for entity in ("schedules", "workflow_schedules"):
        collection(entity).create_index(
            [("enabled", ASCENDING), ("time_of_day", ASCENDING)], name="due"
        )
    collection("workflow_steps").create_index(
        [("workflow_id", ASCENDING), ("step_order", ASCENDING)],
        unique=True,
        name="workflow_step_order_unique",
    )
    collection("workflow_runs").create_index(
        [("workflow_id", ASCENDING), ("status", ASCENDING)], name="workflow_status"
    )
    collection("workflow_run_steps").create_index(
        [("run_id", ASCENDING), ("step_order", ASCENDING)],
        unique=True,
        name="run_step_order_unique",
    )
    collection("workflow_run_steps").create_index(
        [("status", ASCENDING), ("run_after_utc", ASCENDING)], name="due"
    )
    collection("events").create_index(
        [("button_id", ASCENDING), ("status", ASCENDING)], name="button_status"
    )


def _clean(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    result = dict(document)
    result.pop("_id", None)
    return result


def find_all(
    entity: str,
    filters: dict[str, Any] | None = None,
    *,
    sort: Iterable[tuple[str, int]] | None = None,
    limit: int = 0,
    skip: int = 0,
) -> list[dict[str, Any]]:
    cursor = collection(entity).find(filters or {})
    if sort:
        cursor = cursor.sort(list(sort))
    if skip:
        cursor = cursor.skip(skip)
    if limit:
        cursor = cursor.limit(limit)
    return [row for document in cursor if (row := _clean(document)) is not None]


def find_one(
    entity: str,
    filters: dict[str, Any] | None = None,
    *,
    sort: Iterable[tuple[str, int]] | None = None,
) -> dict[str, Any] | None:
    row = collection(entity).find_one(filters or {}, sort=list(sort) if sort else None)
    return _clean(row)


def count(entity: str, filters: dict[str, Any] | None = None) -> int:
    return collection(entity).count_documents(filters or {})


def insert(entity: str, fields: dict[str, Any], *, entity_id: int | None = None) -> int:
    coll = collection(entity)
    counters = counters_collection()
    if entity_id is None:
        counter = counters.find_one_and_update(
            {"_id": entity},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if counter is None:
            raise RuntimeError(f"Could not allocate an ID for {entity}")
        entity_id = int(counter["value"])
    else:
        counters.update_one(
            {"_id": entity},
            {"$max": {"value": entity_id}},
            upsert=True,
        )

    document = {
        "_id": entity_id,
        "id": entity_id,
        "created_at": utc_stamp(),
        **fields,
    }
    try:
        coll.insert_one(document)
    except DuplicateKeyError as exc:
        raise ValueError(f"Duplicate {entity} record") from exc
    return entity_id


def insert_many(entity: str, rows: Iterable[dict[str, Any]]) -> list[int]:
    return [insert(entity, row) for row in rows]


def update(
    entity: str,
    filters: dict[str, Any],
    fields: dict[str, Any],
    *,
    many: bool = False,
) -> int:
    coll = collection(entity)
    operation = coll.update_many if many else coll.update_one
    result = operation(filters, {"$set": fields})
    return int(result.modified_count)


def delete(entity: str, filters: dict[str, Any], *, many: bool = False) -> int:
    coll = collection(entity)
    operation = coll.delete_many if many else coll.delete_one
    result = operation(filters)
    return int(result.deleted_count)


def distinct(entity: str, field: str, filters: dict[str, Any] | None = None) -> list[Any]:
    return collection(entity).distinct(field, filters or {})


def aggregate(entity: str, pipeline: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(collection(entity).aggregate(list(pipeline)))


def replace_all(entity: str, rows: Iterable[dict[str, Any]]) -> int:
    delete(entity, {}, many=True)
    counters_collection().delete_one({"_id": entity})
    inserted = 0
    for row in rows:
        document = dict(row)
        entity_id = int(document.pop("id"))
        insert(entity, document, entity_id=entity_id)
        inserted += 1
    return inserted


def close() -> None:
    global _client, _database
    if _client is not None:
        _client.close()
    _client = None
    _database = None
