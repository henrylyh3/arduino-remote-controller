from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(os.environ.get("SMART_HOME_DB", Path(__file__).resolve().parents[1] / "smart_home.sqlite3"))


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    room TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_seen TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    room TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK(kind IN ('rf_fan', 'ir_ac', 'other')),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS buttons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    signal_type TEXT NOT NULL CHECK(signal_type IN ('rf', 'ir')),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS timers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    button_id INTEGER NOT NULL REFERENCES buttons(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    run_at_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'done', 'failed', 'cancelled')) DEFAULT 'pending',
    error TEXT,
    fired_at_utc TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    button_id INTEGER NOT NULL REFERENCES buttons(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    time_of_day TEXT NOT NULL,
    days TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_date TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL,
    button_id INTEGER NOT NULL REFERENCES buttons(id) ON DELETE CASCADE,
    delay_seconds INTEGER NOT NULL CHECK(delay_seconds >= 0 AND delay_seconds <= 604800),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(workflow_id, step_order)
);

CREATE TABLE IF NOT EXISTS workflow_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    time_of_day TEXT NOT NULL,
    days TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_date TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'done', 'failed', 'cancelled')) DEFAULT 'pending',
    error TEXT,
    started_at_utc TEXT,
    finished_at_utc TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS workflow_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    workflow_step_id INTEGER REFERENCES workflow_steps(id) ON DELETE SET NULL,
    step_order INTEGER NOT NULL,
    button_id INTEGER NOT NULL REFERENCES buttons(id) ON DELETE CASCADE,
    delay_seconds INTEGER NOT NULL CHECK(delay_seconds >= 0 AND delay_seconds <= 604800),
    run_after_utc TEXT,
    status TEXT NOT NULL CHECK(status IN ('waiting', 'pending', 'running', 'done', 'failed', 'cancelled')) DEFAULT 'waiting',
    error TEXT,
    fired_at_utc TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(run_id, step_order)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    button_id INTEGER REFERENCES buttons(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def fetch_all(query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def fetch_one(query: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(query, tuple(params)).fetchone()
    return dict(row) if row else None


def execute(query: str, params: Iterable[Any] = ()) -> int:
    with connect() as conn:
        cursor = conn.execute(query, tuple(params))
        return int(cursor.lastrowid)


def execute_many(query: str, params: Iterable[Iterable[Any]]) -> None:
    with connect() as conn:
        conn.executemany(query, [tuple(item) for item in params])
