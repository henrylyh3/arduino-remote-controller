from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db

APP_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Kuala_Lumpur"))
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


class NodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    room: str = Field(default="", max_length=80)
    base_url: str = Field(min_length=1, max_length=200)
    enabled: bool = True


class DeviceIn(BaseModel):
    node_id: int
    name: str = Field(min_length=1, max_length=80)
    room: str = Field(default="", max_length=80)
    kind: Literal["rf_fan", "ir_ac", "other"]
    notes: str = Field(default="", max_length=300)


class ButtonIn(BaseModel):
    device_id: int
    name: str = Field(min_length=1, max_length=80)
    signal_type: Literal["rf", "ir"]
    payload: dict[str, Any]


class LearnSignalIn(BaseModel):
    node_id: int
    name: str = Field(min_length=1, max_length=80)
    signal_type: Literal["rf", "ir"]
    timeout_ms: int = Field(default=8000, ge=1000, le=30000)


class TimerIn(BaseModel):
    button_id: int
    seconds: int = Field(gt=0, le=7 * 24 * 60 * 60)
    name: str = Field(default="Timer", min_length=1, max_length=80)


class ScheduleIn(BaseModel):
    button_id: int
    name: str = Field(default="Schedule", min_length=1, max_length=80)
    time_of_day: str = Field(pattern=r"^\d{2}:\d{2}$")
    days: list[int] = Field(min_length=1, max_length=7)
    enabled: bool = True


class WorkflowStepIn(BaseModel):
    button_id: int
    delay_seconds: int = Field(ge=0, le=7 * 24 * 60 * 60)


class WorkflowIn(BaseModel):
    name: str = Field(default="Workflow", min_length=1, max_length=80)
    steps: list[WorkflowStepIn] = Field(min_length=1, max_length=20)


class WorkflowScheduleIn(BaseModel):
    workflow_id: int
    name: str = Field(default="Workflow schedule", min_length=1, max_length=80)
    time_of_day: str = Field(pattern=r"^\d{2}:\d{2}$")
    days: list[int] = Field(min_length=1, max_length=7)
    enabled: bool = True


class CommandError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_now() -> datetime:
    return utc_now().astimezone(APP_TIMEZONE)


def clean_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        base_url = f"http://{base_url}"
    return base_url


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def json_loads(value: str) -> Any:
    return json.loads(value)


def normalize_schedule(time_of_day: str, days: list[int]) -> tuple[str, list[int]]:
    if not re.match(r"^\d{2}:\d{2}$", time_of_day):
        raise HTTPException(status_code=422, detail="time_of_day must be HH:MM")
    hour, minute = [int(part) for part in time_of_day.split(":")]
    if hour > 23 or minute > 59:
        raise HTTPException(status_code=422, detail="time_of_day must be valid 24-hour time")
    unique_days = sorted(set(days))
    if any(day < 0 or day > 6 for day in unique_days):
        raise HTTPException(status_code=422, detail="days must use 0=Mon through 6=Sun")
    return time_of_day, unique_days


def ensure_button_exists(button_id: int) -> dict[str, Any]:
    row = db.fetch_one(
        """
        SELECT
            buttons.*,
            devices.name AS device_name,
            devices.kind AS device_kind,
            nodes.base_url AS node_base_url,
            nodes.enabled AS node_enabled
        FROM buttons
        JOIN devices ON devices.id = buttons.device_id
        JOIN nodes ON nodes.id = devices.node_id
        WHERE buttons.id = ?
        """,
        (button_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Button not found")
    return row


def ensure_signal_device(node_id: int) -> int:
    node = db.fetch_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    device = db.fetch_one(
        "SELECT id FROM devices WHERE node_id = ? AND name = ?",
        (node_id, "Signals"),
    )
    if device:
        return int(device["id"])

    return db.execute(
        """
        INSERT INTO devices (node_id, name, room, kind, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (node_id, "Signals", node["room"], "other", "Auto-created for learned signals"),
    )


def validate_rf_payload(payload: dict[str, Any]) -> dict[str, int]:
    try:
        return {
            "code": int(payload["code"]),
            "bits": int(payload.get("bits", 24)),
            "protocol": int(payload.get("protocol", 1)),
            "pulse_length": int(payload.get("pulse_length", 0)),
            "repeat": int(payload.get("repeat", 6)),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise CommandError("RF payload needs code, bits, protocol, pulse_length, repeat") from exc


def validate_ir_payload(payload: dict[str, Any]) -> tuple[list[int], int, int]:
    raw = payload.get("raw")
    if not isinstance(raw, list) or not raw:
        raise CommandError("IR payload needs non-empty raw array")
    try:
        durations = [int(item) for item in raw]
        khz = int(payload.get("khz", 38))
        repeat = int(payload.get("repeat", 1))
    except (TypeError, ValueError) as exc:
        raise CommandError("IR payload raw, khz, and repeat must be numeric") from exc
    if any(duration <= 0 for duration in durations):
        raise CommandError("IR raw durations must be positive microseconds")
    return durations, khz, repeat


async def send_button_to_node(button_id: int) -> dict[str, Any]:
    button = ensure_button_exists(button_id)
    if not button["node_enabled"]:
        raise CommandError("Node is disabled")

    payload = json_loads(button["payload"])
    base_url = clean_base_url(button["node_base_url"])

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            if button["signal_type"] == "rf":
                rf_payload = validate_rf_payload(payload)
                response = await client.get(f"{base_url}/send/rf", params=rf_payload)
            else:
                raw, khz, repeat = validate_ir_payload(payload)
                response = await client.post(
                    f"{base_url}/send/ir/raw",
                    params={"khz": khz, "repeat": repeat},
                    content=",".join(str(item) for item in raw),
                    headers={"content-type": "text/plain"},
                )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise CommandError(f"Node returned HTTP {exc.response.status_code}: {exc.response.text[:160]}") from exc
    except httpx.HTTPError as exc:
        raise CommandError(f"Node request failed: {exc}") from exc

    message = f"Sent {button['name']} to {button['device_name']}"
    db.execute(
        "INSERT INTO events (button_id, status, message) VALUES (?, ?, ?)",
        (button_id, "sent", message),
    )
    return {"ok": True, "message": message}


def create_workflow_run_record(workflow_id: int) -> dict[str, Any]:
    workflow = db.fetch_one("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    steps = db.fetch_all(
        """
        SELECT *
        FROM workflow_steps
        WHERE workflow_id = ?
        ORDER BY step_order
        """,
        (workflow_id,),
    )
    if not steps:
        raise HTTPException(status_code=422, detail="Workflow has no steps")

    first_run_at = utc_stamp(utc_now() + timedelta(seconds=steps[0]["delay_seconds"]))
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO workflow_runs (workflow_id, name, status) VALUES (?, ?, 'pending')",
            (workflow_id, workflow["name"]),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO workflow_run_steps
                (run_id, workflow_step_id, step_order, button_id, delay_seconds, run_after_utc, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    step["id"],
                    step["step_order"],
                    step["button_id"],
                    step["delay_seconds"],
                    first_run_at if index == 0 else None,
                    "pending" if index == 0 else "waiting",
                )
                for index, step in enumerate(steps)
            ],
        )
    return {"id": run_id, "first_run_at_utc": first_run_at, "name": workflow["name"]}


async def run_due_timers() -> None:
    due = db.fetch_all(
        """
        SELECT id, button_id
        FROM timers
        WHERE status = 'pending' AND run_at_utc <= ?
        ORDER BY run_at_utc ASC
        LIMIT 10
        """,
        (utc_stamp(),),
    )
    for timer in due:
        db.execute("UPDATE timers SET status = 'running' WHERE id = ? AND status = 'pending'", (timer["id"],))
        try:
            await send_button_to_node(timer["button_id"])
            db.execute(
                "UPDATE timers SET status = 'done', fired_at_utc = ?, error = NULL WHERE id = ?",
                (utc_stamp(), timer["id"]),
            )
        except Exception as exc:
            message = str(exc)
            db.execute(
                "UPDATE timers SET status = 'failed', fired_at_utc = ?, error = ? WHERE id = ?",
                (utc_stamp(), message, timer["id"]),
            )
            db.execute(
                "INSERT INTO events (button_id, status, message) VALUES (?, ?, ?)",
                (timer["button_id"], "failed", message),
            )


async def run_due_schedules() -> None:
    now = local_now()
    current_time = now.strftime("%H:%M")
    today = now.date().isoformat()
    weekday = now.weekday()
    schedules = db.fetch_all(
        """
        SELECT id, button_id, days, last_run_date
        FROM schedules
        WHERE enabled = 1 AND time_of_day = ?
        """,
        (current_time,),
    )
    for schedule in schedules:
        days = {int(day) for day in schedule["days"].split(",") if day != ""}
        if weekday not in days or schedule["last_run_date"] == today:
            continue
        db.execute("UPDATE schedules SET last_run_date = ? WHERE id = ?", (today, schedule["id"]))
        try:
            await send_button_to_node(schedule["button_id"])
        except Exception as exc:
            db.execute(
                "INSERT INTO events (button_id, status, message) VALUES (?, ?, ?)",
                (schedule["button_id"], "failed", str(exc)),
            )


async def run_due_workflow_schedules() -> None:
    now = local_now()
    current_time = now.strftime("%H:%M")
    today = now.date().isoformat()
    weekday = now.weekday()
    schedules = db.fetch_all(
        """
        SELECT id, workflow_id, name, days, last_run_date
        FROM workflow_schedules
        WHERE enabled = 1 AND time_of_day = ?
        """,
        (current_time,),
    )
    for schedule in schedules:
        days = {int(day) for day in schedule["days"].split(",") if day != ""}
        if weekday not in days or schedule["last_run_date"] == today:
            continue
        db.execute("UPDATE workflow_schedules SET last_run_date = ? WHERE id = ?", (today, schedule["id"]))
        try:
            run = create_workflow_run_record(schedule["workflow_id"])
            db.execute(
                "INSERT INTO events (button_id, status, message) VALUES (?, ?, ?)",
                (None, "workflow", f"Started workflow {run['name']} from schedule {schedule['name']}"),
            )
        except Exception as exc:
            db.execute(
                "INSERT INTO events (button_id, status, message) VALUES (?, ?, ?)",
                (None, "failed", f"Workflow schedule {schedule['name']} failed: {exc}"),
            )


async def run_due_workflow_steps() -> None:
    due = db.fetch_all(
        """
        SELECT workflow_run_steps.*, workflow_runs.name AS run_name
        FROM workflow_run_steps
        JOIN workflow_runs ON workflow_runs.id = workflow_run_steps.run_id
        WHERE workflow_run_steps.status = 'pending'
            AND workflow_run_steps.run_after_utc <= ?
            AND workflow_runs.status IN ('pending', 'running')
        ORDER BY workflow_run_steps.run_after_utc ASC
        LIMIT 5
        """,
        (utc_stamp(),),
    )

    for step in due:
        db.execute(
            """
            UPDATE workflow_run_steps
            SET status = 'running'
            WHERE id = ? AND status = 'pending'
            """,
            (step["id"],),
        )
        db.execute(
            """
            UPDATE workflow_runs
            SET status = 'running', started_at_utc = COALESCE(started_at_utc, ?)
            WHERE id = ? AND status IN ('pending', 'running')
            """,
            (utc_stamp(), step["run_id"]),
        )

        try:
            await send_button_to_node(step["button_id"])
            db.execute(
                """
                UPDATE workflow_run_steps
                SET status = 'done', fired_at_utc = ?, error = NULL
                WHERE id = ?
                """,
                (utc_stamp(), step["id"]),
            )

            next_step = db.fetch_one(
                """
                SELECT id, delay_seconds
                FROM workflow_run_steps
                WHERE run_id = ? AND step_order = ?
                """,
                (step["run_id"], step["step_order"] + 1),
            )
            if next_step:
                db.execute(
                    """
                    UPDATE workflow_run_steps
                    SET status = 'pending', run_after_utc = ?
                    WHERE id = ? AND status = 'waiting'
                    """,
                    (utc_stamp(utc_now() + timedelta(seconds=next_step["delay_seconds"])), next_step["id"]),
                )
            else:
                db.execute(
                    """
                    UPDATE workflow_runs
                    SET status = 'done', finished_at_utc = ?, error = NULL
                    WHERE id = ?
                    """,
                    (utc_stamp(), step["run_id"]),
                )
        except Exception as exc:
            message = str(exc)
            db.execute(
                """
                UPDATE workflow_run_steps
                SET status = 'failed', fired_at_utc = ?, error = ?
                WHERE id = ?
                """,
                (utc_stamp(), message, step["id"]),
            )
            db.execute(
                """
                UPDATE workflow_run_steps
                SET status = 'cancelled'
                WHERE run_id = ? AND status IN ('waiting', 'pending')
                """,
                (step["run_id"],),
            )
            db.execute(
                """
                UPDATE workflow_runs
                SET status = 'failed', finished_at_utc = ?, error = ?
                WHERE id = ?
                """,
                (utc_stamp(), message, step["run_id"]),
            )
            db.execute(
                "INSERT INTO events (button_id, status, message) VALUES (?, ?, ?)",
                (step["button_id"], "failed", f"Workflow {step['run_name']} failed: {message}"),
            )


async def scheduler_loop() -> None:
    while True:
        try:
            await run_due_timers()
            await run_due_schedules()
            await run_due_workflow_schedules()
            await run_due_workflow_steps()
        except Exception as exc:
            print(f"scheduler error: {exc}", flush=True)
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Local RF/IR Controller", lifespan=lifespan)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time_utc": utc_stamp(), "timezone": str(APP_TIMEZONE)}


@app.get("/api/state")
def state() -> dict[str, Any]:
    nodes = db.fetch_all("SELECT * FROM nodes ORDER BY room, name")
    devices = db.fetch_all("SELECT * FROM devices ORDER BY room, name")
    buttons = db.fetch_all("SELECT * FROM buttons ORDER BY name")
    timers = db.fetch_all("SELECT * FROM timers ORDER BY created_at DESC LIMIT 50")
    schedules = db.fetch_all("SELECT * FROM schedules ORDER BY time_of_day, name")
    workflows = db.fetch_all("SELECT * FROM workflows ORDER BY created_at DESC")
    workflow_steps = db.fetch_all("SELECT * FROM workflow_steps ORDER BY workflow_id, step_order")
    workflow_schedules = db.fetch_all("SELECT * FROM workflow_schedules ORDER BY time_of_day, name")
    workflow_runs = db.fetch_all("SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT 30")
    workflow_run_steps = db.fetch_all(
        "SELECT * FROM workflow_run_steps ORDER BY run_id, step_order"
    )
    events = db.fetch_all("SELECT * FROM events ORDER BY created_at DESC LIMIT 40")
    stats = {
        row["button_id"]: row
        for row in db.fetch_all(
            """
            SELECT button_id, COUNT(*) AS press_count, MAX(created_at) AS last_pressed
            FROM events
            WHERE status = 'sent' AND button_id IS NOT NULL
            GROUP BY button_id
            """
        )
    }
    for button in buttons:
        button["payload"] = json_loads(button["payload"])
        button["stats"] = stats.get(button["id"], {"press_count": 0, "last_pressed": None})
    for node in nodes:
        node["enabled"] = bool(node["enabled"])
    for schedule in schedules:
        schedule["enabled"] = bool(schedule["enabled"])
        schedule["days"] = [int(day) for day in schedule["days"].split(",") if day != ""]
    for schedule in workflow_schedules:
        schedule["enabled"] = bool(schedule["enabled"])
        schedule["days"] = [int(day) for day in schedule["days"].split(",") if day != ""]
    return {
        "nodes": nodes,
        "devices": devices,
        "buttons": buttons,
        "timers": timers,
        "schedules": schedules,
        "workflows": workflows,
        "workflow_steps": workflow_steps,
        "workflow_schedules": workflow_schedules,
        "workflow_runs": workflow_runs,
        "workflow_run_steps": workflow_run_steps,
        "events": events,
        "timezone": str(APP_TIMEZONE),
    }


@app.post("/api/nodes")
def create_node(node: NodeIn) -> dict[str, Any]:
    node_id = db.execute(
        "INSERT INTO nodes (name, room, base_url, enabled) VALUES (?, ?, ?, ?)",
        (node.name.strip(), node.room.strip(), clean_base_url(node.base_url), int(node.enabled)),
    )
    return {"id": node_id}


@app.post("/api/nodes/{node_id}/ping")
async def ping_node(node_id: int) -> dict[str, Any]:
    node = db.fetch_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{clean_base_url(node['base_url'])}/health")
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Node ping failed: {exc}") from exc
    db.execute("UPDATE nodes SET last_seen = ? WHERE id = ?", (utc_stamp(), node_id))
    return {"ok": True, "node": data}


@app.post("/api/devices")
def create_device(device: DeviceIn) -> dict[str, Any]:
    if not db.fetch_one("SELECT id FROM nodes WHERE id = ?", (device.node_id,)):
        raise HTTPException(status_code=404, detail="Node not found")
    device_id = db.execute(
        "INSERT INTO devices (node_id, name, room, kind, notes) VALUES (?, ?, ?, ?, ?)",
        (device.node_id, device.name.strip(), device.room.strip(), device.kind, device.notes.strip()),
    )
    return {"id": device_id}


@app.post("/api/buttons")
def create_button(button: ButtonIn) -> dict[str, Any]:
    if not db.fetch_one("SELECT id FROM devices WHERE id = ?", (button.device_id,)):
        raise HTTPException(status_code=404, detail="Device not found")
    if button.signal_type == "rf":
        validate_rf_payload(button.payload)
    else:
        validate_ir_payload(button.payload)
    button_id = db.execute(
        "INSERT INTO buttons (device_id, name, signal_type, payload) VALUES (?, ?, ?, ?)",
        (button.device_id, button.name.strip(), button.signal_type, json_dumps(button.payload)),
    )
    return {"id": button_id}


@app.post("/api/buttons/{button_id}/press")
async def press_button(button_id: int) -> dict[str, Any]:
    try:
        return await send_button_to_node(button_id)
    except CommandError as exc:
        db.execute(
            "INSERT INTO events (button_id, status, message) VALUES (?, ?, ?)",
            (button_id, "failed", str(exc)),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def capture_from_node(
    node_id: int,
    signal_type: Literal["rf", "ir"],
    timeout_ms: int,
) -> dict[str, Any]:
    node = db.fetch_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        async with httpx.AsyncClient(timeout=(timeout_ms / 1000) + 4) as client:
            response = await client.get(
                f"{clean_base_url(node['base_url'])}/capture/{signal_type}",
                params={"timeout_ms": timeout_ms},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Capture failed: {exc.response.text[:160]}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Capture failed: {exc}") from exc

    if signal_type == "rf":
        normalized = validate_rf_payload(payload)
    else:
        raw, khz, repeat = validate_ir_payload({**payload, "repeat": payload.get("repeat", 1)})
        normalized = {"raw": raw, "khz": khz, "repeat": repeat}
    db.execute("UPDATE nodes SET last_seen = ? WHERE id = ?", (utc_stamp(), node_id))
    return normalized


@app.post("/api/nodes/{node_id}/capture/{signal_type}")
async def capture_signal(
    node_id: int,
    signal_type: Literal["rf", "ir"],
    timeout_ms: int = Query(default=8000, ge=1000, le=30000),
) -> dict[str, Any]:
    normalized = await capture_from_node(node_id, signal_type, timeout_ms)
    return {"payload": normalized}


@app.post("/api/signals/learn")
async def learn_signal(signal: LearnSignalIn) -> dict[str, Any]:
    normalized = await capture_from_node(signal.node_id, signal.signal_type, signal.timeout_ms)
    device_id = ensure_signal_device(signal.node_id)
    button_id = db.execute(
        "INSERT INTO buttons (device_id, name, signal_type, payload) VALUES (?, ?, ?, ?)",
        (device_id, signal.name.strip(), signal.signal_type, json_dumps(normalized)),
    )
    return {"id": button_id, "name": signal.name.strip(), "payload": normalized}


@app.post("/api/timers")
def create_timer(timer: TimerIn) -> dict[str, Any]:
    ensure_button_exists(timer.button_id)
    run_at = utc_stamp(utc_now() + timedelta(seconds=timer.seconds))
    timer_id = db.execute(
        "INSERT INTO timers (button_id, name, run_at_utc) VALUES (?, ?, ?)",
        (timer.button_id, timer.name.strip(), run_at),
    )
    return {"id": timer_id, "run_at_utc": run_at}


@app.post("/api/timers/{timer_id}/cancel")
def cancel_timer(timer_id: int) -> dict[str, Any]:
    db.execute("UPDATE timers SET status = 'cancelled' WHERE id = ? AND status IN ('pending', 'running')", (timer_id,))
    return {"ok": True}


@app.post("/api/workflows")
def create_workflow(workflow: WorkflowIn) -> dict[str, Any]:
    steps = workflow.steps
    for step in steps:
        ensure_button_exists(step.button_id)

    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO workflows (name) VALUES (?)",
            (workflow.name.strip(),),
        )
        workflow_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO workflow_steps (workflow_id, step_order, button_id, delay_seconds)
            VALUES (?, ?, ?, ?)
            """,
            [
                (workflow_id, index, step.button_id, step.delay_seconds)
                for index, step in enumerate(steps, start=1)
            ],
        )

    return {"id": workflow_id}


@app.put("/api/workflows/{workflow_id}")
def update_workflow(workflow_id: int, workflow: WorkflowIn) -> dict[str, Any]:
    if not db.fetch_one("SELECT id FROM workflows WHERE id = ?", (workflow_id,)):
        raise HTTPException(status_code=404, detail="Workflow not found")

    steps = workflow.steps
    for step in steps:
        ensure_button_exists(step.button_id)

    with db.connect() as conn:
        conn.execute(
            "UPDATE workflows SET name = ? WHERE id = ?",
            (workflow.name.strip(), workflow_id),
        )
        conn.execute("DELETE FROM workflow_steps WHERE workflow_id = ?", (workflow_id,))
        conn.executemany(
            """
            INSERT INTO workflow_steps (workflow_id, step_order, button_id, delay_seconds)
            VALUES (?, ?, ?, ?)
            """,
            [
                (workflow_id, index, step.button_id, step.delay_seconds)
                for index, step in enumerate(steps, start=1)
            ],
        )

    return {"id": workflow_id}


@app.post("/api/workflows/{workflow_id}/run")
def run_workflow(workflow_id: int) -> dict[str, Any]:
    return create_workflow_run_record(workflow_id)


@app.post("/api/workflow-runs/{run_id}/cancel")
def cancel_workflow_run(run_id: int) -> dict[str, Any]:
    run = db.fetch_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    db.execute(
        """
        UPDATE workflow_runs
        SET status = 'cancelled', finished_at_utc = ?
        WHERE id = ? AND status IN ('pending', 'running')
        """,
        (utc_stamp(), run_id),
    )
    db.execute(
        """
        UPDATE workflow_run_steps
        SET status = 'cancelled'
        WHERE run_id = ? AND status IN ('waiting', 'pending', 'running')
        """,
        (run_id,),
    )
    return {"ok": True}


@app.post("/api/workflow-schedules")
def create_workflow_schedule(schedule: WorkflowScheduleIn) -> dict[str, Any]:
    if not db.fetch_one("SELECT id FROM workflows WHERE id = ?", (schedule.workflow_id,)):
        raise HTTPException(status_code=404, detail="Workflow not found")
    time_of_day, unique_days = normalize_schedule(schedule.time_of_day, schedule.days)
    schedule_id = db.execute(
        """
        INSERT INTO workflow_schedules (workflow_id, name, time_of_day, days, enabled)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            schedule.workflow_id,
            schedule.name.strip(),
            time_of_day,
            ",".join(str(day) for day in unique_days),
            int(schedule.enabled),
        ),
    )
    return {"id": schedule_id}


@app.post("/api/workflow-schedules/{schedule_id}/toggle")
def toggle_workflow_schedule(schedule_id: int) -> dict[str, Any]:
    schedule = db.fetch_one("SELECT enabled FROM workflow_schedules WHERE id = ?", (schedule_id,))
    if not schedule:
        raise HTTPException(status_code=404, detail="Workflow schedule not found")
    enabled = 0 if schedule["enabled"] else 1
    db.execute("UPDATE workflow_schedules SET enabled = ? WHERE id = ?", (enabled, schedule_id))
    return {"ok": True, "enabled": bool(enabled)}


@app.post("/api/schedules")
def create_schedule(schedule: ScheduleIn) -> dict[str, Any]:
    ensure_button_exists(schedule.button_id)
    time_of_day, unique_days = normalize_schedule(schedule.time_of_day, schedule.days)
    schedule_id = db.execute(
        """
        INSERT INTO schedules (button_id, name, time_of_day, days, enabled)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            schedule.button_id,
            schedule.name.strip(),
            time_of_day,
            ",".join(str(day) for day in unique_days),
            int(schedule.enabled),
        ),
    )
    return {"id": schedule_id}


@app.post("/api/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: int) -> dict[str, Any]:
    schedule = db.fetch_one("SELECT enabled FROM schedules WHERE id = ?", (schedule_id,))
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    enabled = 0 if schedule["enabled"] else 1
    db.execute("UPDATE schedules SET enabled = ? WHERE id = ?", (enabled, schedule_id))
    return {"ok": True, "enabled": bool(enabled)}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
