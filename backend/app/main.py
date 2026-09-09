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
NODE_HEALTH_INTERVAL_SECONDS = 15
NODE_HEALTH_TIMEOUT_SECONDS = 2
node_health_cache: dict[int, dict[str, Any]] = {}
active_capture_cancellations: dict[int, asyncio.Event] = {}


class NodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    room: str = Field(default="", max_length=80)
    base_url: str = Field(min_length=1, max_length=200)
    enabled: bool = True


class NodeUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str | None = Field(default=None, min_length=1, max_length=200)


class NodeOrderIn(BaseModel):
    node_ids: list[int] = Field(min_length=1)


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


class CapturedSignalIn(BaseModel):
    node_id: int
    name: str = Field(min_length=1, max_length=80)
    signal_type: Literal["rf", "ir"]
    payload: dict[str, Any]


class AcControllerCommandIn(BaseModel):
    temperature: int = Field(ge=16, le=30)
    fan: Literal["auto", "1", "2", "3"]
    swing: bool
    power_toggle: bool = False


class SignalUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class StarIn(BaseModel):
    starred: bool


class TimerIn(BaseModel):
    button_id: int
    seconds: int = Field(gt=0, le=7 * 24 * 60 * 60)
    name: str = Field(default="Timer", min_length=1, max_length=80)


class TimerPresetIn(BaseModel):
    target_kind: Literal["signal", "workflow"]
    target_id: int = Field(gt=0)
    seconds: int = Field(gt=0, le=7 * 24 * 60 * 60)


class ScheduleIn(BaseModel):
    button_id: int
    name: str = Field(default="Schedule", min_length=1, max_length=80)
    time_of_day: str = Field(pattern=r"^\d{2}:\d{2}$")
    days: list[int] = Field(min_length=1, max_length=7)
    enabled: bool = True


class ScheduleItemIn(BaseModel):
    target_kind: Literal["signal", "workflow"]
    target_id: int = Field(gt=0)
    name: str = Field(default="Schedule", min_length=1, max_length=80)
    time_of_day: str = Field(pattern=r"^\d{2}:\d{2}$")
    days: list[int] = Field(min_length=1, max_length=7)
    enabled: bool = True


class EnabledIn(BaseModel):
    enabled: bool


class WorkflowStepIn(BaseModel):
    button_id: int
    delay_seconds: int = Field(ge=0, le=7 * 24 * 60 * 60)


class WorkflowIn(BaseModel):
    name: str = Field(default="Workflow", min_length=1, max_length=80)
    steps: list[WorkflowStepIn] = Field(min_length=1, max_length=20)


class WorkflowRunIn(BaseModel):
    delay_seconds: int = Field(default=0, ge=0, le=7 * 24 * 60 * 60)
    name: str | None = Field(default=None, min_length=1, max_length=80)


class WorkflowScheduleIn(BaseModel):
    workflow_id: int
    name: str = Field(default="Workflow schedule", min_length=1, max_length=80)
    time_of_day: str = Field(pattern=r"^\d{2}:\d{2}$")
    days: list[int] = Field(min_length=1, max_length=7)
    enabled: bool = True


class CommandError(RuntimeError):
    pass


class CaptureCancelled(RuntimeError):
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


def uint8_to_bcd(value: int) -> int:
    return ((value // 10) << 4) | (value % 10)


def daikin64_payload(command: AcControllerCommandIn, now: datetime | None = None) -> dict[str, Any]:
    local_time = (now or local_now()).astimezone(APP_TIMEZONE)
    fan_codes = {"auto": 0x1, "1": 0x8, "2": 0x4, "3": 0x2}
    flags = 0x04 | int(command.swing) | (0x08 if command.power_toggle else 0)
    state = [
        0x16,
        (fan_codes[command.fan] << 4) | 0x02,
        uint8_to_bcd(local_time.minute),
        uint8_to_bcd(local_time.hour),
        0x10,
        0x10,
        uint8_to_bcd(command.temperature),
        flags,
    ]
    checksum = sum((byte & 0x0F) + (byte >> 4) for byte in state[:7]) + flags
    state[7] = ((checksum & 0x0F) << 4) | flags

    raw = [9800, 9800, 9800, 9800, 4600, 2500]
    for byte in state:
        for bit in range(8):
            raw.extend((350, 954 if byte & (1 << bit) else 382))
    raw.extend((350, 20300, 4600))
    return {"raw": raw, "khz": 38, "repeat": 1, "state": state}


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


def rf_payload_matches(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    try:
        return all(
            int(existing[key]) == int(candidate[key])
            for key in ("code", "bits", "protocol")
        )
    except (KeyError, TypeError, ValueError):
        return False


def ir_duration_matches(existing: int, candidate: int) -> bool:
    tolerance = max(200, int(max(existing, candidate) * 0.25))
    return abs(existing - candidate) <= tolerance


def ir_payload_matches(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    try:
        existing_raw, existing_khz, _ = validate_ir_payload(existing)
        candidate_raw, candidate_khz, _ = validate_ir_payload(candidate)
    except CommandError:
        return False
    if existing_khz != candidate_khz or len(existing_raw) != len(candidate_raw):
        return False
    return all(
        ir_duration_matches(existing_duration, candidate_duration)
        for existing_duration, candidate_duration in zip(existing_raw, candidate_raw)
    )


def signal_payload_matches(
    signal_type: Literal["rf", "ir"],
    existing_payload: dict[str, Any],
    candidate_payload: dict[str, Any],
) -> bool:
    if signal_type == "rf":
        return rf_payload_matches(existing_payload, candidate_payload)
    return ir_payload_matches(existing_payload, candidate_payload)


def find_duplicate_signal(
    node_id: int,
    signal_type: Literal["rf", "ir"],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    buttons = db.fetch_all(
        """
        SELECT
            buttons.id,
            buttons.name,
            buttons.signal_type,
            buttons.payload,
            devices.name AS device_name,
            nodes.name AS node_name
        FROM buttons
        JOIN devices ON devices.id = buttons.device_id
        JOIN nodes ON nodes.id = devices.node_id
        WHERE devices.node_id = ? AND buttons.signal_type = ?
        ORDER BY buttons.created_at ASC, buttons.id ASC
        """,
        (node_id, signal_type),
    )
    for button in buttons:
        if signal_payload_matches(signal_type, json_loads(button["payload"]), payload):
            return {
                "id": button["id"],
                "name": button["name"],
                "device_name": button["device_name"],
                "node_name": button["node_name"],
                "signal_type": button["signal_type"],
            }
    return None


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


async def send_ac_controller_command(
    controller_id: int,
    command: AcControllerCommandIn,
) -> dict[str, Any]:
    controller = db.fetch_one(
        """
        SELECT ac_controllers.*, nodes.base_url AS node_base_url,
               nodes.enabled AS node_enabled, nodes.name AS node_name
        FROM ac_controllers
        JOIN nodes ON nodes.id = ac_controllers.node_id
        WHERE ac_controllers.id = ?
        """,
        (controller_id,),
    )
    if not controller:
        raise HTTPException(status_code=404, detail="Aircond controller not found")
    if not controller["node_enabled"]:
        raise CommandError("Node is disabled")

    payload = daikin64_payload(command)
    raw, khz, repeat = validate_ir_payload(payload)
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                f"{clean_base_url(controller['node_base_url'])}/send/ir/raw",
                params={"khz": khz, "repeat": repeat},
                content=",".join(str(item) for item in raw),
                headers={"content-type": "text/plain"},
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise CommandError(
            f"Node returned HTTP {exc.response.status_code}: {exc.response.text[:160]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise CommandError(f"Node request failed: {exc}") from exc

    settings = (
        f"{command.temperature} C, Fan {command.fan.title()}, "
        f"Swing {'on' if command.swing else 'off'}"
    )
    last_command = f"Power toggle - {settings}" if command.power_toggle else settings
    sent_at = utc_stamp()
    db.execute(
        """
        UPDATE ac_controllers
        SET temperature = ?, fan = ?, swing = ?, last_command = ?, last_sent_at = ?
        WHERE id = ?
        """,
        (
            command.temperature,
            command.fan,
            int(command.swing),
            last_command,
            sent_at,
            controller_id,
        ),
    )
    message = f"Aircond controller sent {last_command} to {controller['node_name']}"
    db.execute(
        "INSERT INTO events (button_id, status, message) VALUES (NULL, ?, ?)",
        ("sent", message),
    )
    updated = db.fetch_one("SELECT * FROM ac_controllers WHERE id = ?", (controller_id,))
    if updated:
        updated["swing"] = bool(updated["swing"])
    return {"ok": True, "message": message, "controller": updated}


def create_workflow_run_record(
    workflow_id: int,
    *,
    start_delay_seconds: int = 0,
    run_name: str | None = None,
) -> dict[str, Any]:
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

    first_run_at = utc_stamp(
        utc_now() + timedelta(seconds=start_delay_seconds + steps[0]["delay_seconds"])
    )
    name = run_name.strip() if run_name else workflow["name"]
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO workflow_runs (workflow_id, name, status) VALUES (?, ?, 'pending')",
            (workflow_id, name),
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
    return {"id": run_id, "first_run_at_utc": first_run_at, "name": name}


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


async def run_due_timer_presets() -> None:
    due = db.fetch_all(
        """
        SELECT id, button_id, workflow_id
        FROM timer_presets
        WHERE active = 1 AND run_at_utc <= ?
        ORDER BY run_at_utc ASC
        LIMIT 10
        """,
        (utc_stamp(),),
    )
    for timer in due:
        db.execute(
            "UPDATE timer_presets SET active = 0, run_at_utc = NULL WHERE id = ? AND active = 1",
            (timer["id"],),
        )
        try:
            if timer["button_id"] is not None:
                await send_button_to_node(timer["button_id"])
            else:
                run = create_workflow_run_record(timer["workflow_id"])
                db.execute(
                    "INSERT INTO events (button_id, status, message) VALUES (NULL, ?, ?)",
                    ("workflow", f"Started workflow {run['name']} from timer"),
                )
            db.execute("UPDATE timer_presets SET error = NULL WHERE id = ?", (timer["id"],))
        except Exception as exc:
            message = str(exc)
            db.execute("UPDATE timer_presets SET error = ? WHERE id = ?", (message, timer["id"]))
            db.execute(
                "INSERT INTO events (button_id, status, message) VALUES (?, ?, ?)",
                (timer["button_id"], "failed", f"Timer failed: {message}"),
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
            await run_due_timer_presets()
            await run_due_schedules()
            await run_due_workflow_schedules()
            await run_due_workflow_steps()
        except Exception as exc:
            print(f"scheduler error: {exc}", flush=True)
        await asyncio.sleep(1)


async def probe_node_health(node: dict[str, Any], client: httpx.AsyncClient) -> dict[str, Any]:
    checked_at = utc_stamp()
    if not node["enabled"]:
        return {"status": "disabled", "checked_at": checked_at, "latency_ms": None}

    started_at = asyncio.get_running_loop().time()
    try:
        response = await client.get(f"{clean_base_url(node['base_url'])}/health")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return {
            "status": "offline",
            "checked_at": checked_at,
            "latency_ms": None,
            "error": str(exc) or type(exc).__name__,
        }

    latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
    return {"status": "online", "checked_at": checked_at, "latency_ms": latency_ms}


async def refresh_node_health(client: httpx.AsyncClient) -> None:
    global node_health_cache
    nodes = db.fetch_all("SELECT id, base_url, enabled FROM nodes ORDER BY id")
    results = await asyncio.gather(*(probe_node_health(node, client) for node in nodes))
    node_health_cache = {node["id"]: result for node, result in zip(nodes, results)}


async def node_health_loop() -> None:
    async with httpx.AsyncClient(timeout=NODE_HEALTH_TIMEOUT_SECONDS) as client:
        while True:
            try:
                await refresh_node_health(client)
            except Exception as exc:
                print(f"node health error: {exc}", flush=True)
            await asyncio.sleep(NODE_HEALTH_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    tasks = [asyncio.create_task(scheduler_loop()), asyncio.create_task(node_health_loop())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Local RF/IR Controller", lifespan=lifespan)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time_utc": utc_stamp(), "timezone": str(APP_TIMEZONE)}


@app.get("/api/events")
def events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    total_row = db.fetch_one("SELECT COUNT(*) AS total FROM events")
    total = int(total_row["total"] if total_row else 0)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    items = db.fetch_all(
        """
        SELECT * FROM events
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (page_size, (current_page - 1) * page_size),
    )
    return {
        "items": items,
        "page": current_page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


@app.get("/api/state")
def state() -> dict[str, Any]:
    nodes = db.fetch_all("SELECT * FROM nodes ORDER BY sort_order, id")
    devices = db.fetch_all("SELECT * FROM devices ORDER BY room, name")
    buttons = db.fetch_all("SELECT * FROM buttons ORDER BY name")
    ac_controllers = db.fetch_all("SELECT * FROM ac_controllers ORDER BY id")
    timers = db.fetch_all("SELECT * FROM timers ORDER BY created_at DESC LIMIT 50")
    timer_presets = db.fetch_all("SELECT * FROM timer_presets ORDER BY created_at DESC")
    schedules = db.fetch_all("SELECT * FROM schedules ORDER BY time_of_day, name")
    workflows = db.fetch_all("SELECT * FROM workflows ORDER BY created_at DESC")
    workflow_steps = db.fetch_all("SELECT * FROM workflow_steps ORDER BY workflow_id, step_order")
    workflow_schedules = db.fetch_all("SELECT * FROM workflow_schedules ORDER BY time_of_day, name")
    workflow_runs = db.fetch_all("SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT 30")
    workflow_run_steps = db.fetch_all(
        "SELECT * FROM workflow_run_steps ORDER BY run_id, step_order"
    )
    events = db.fetch_all("SELECT * FROM events ORDER BY created_at DESC, id DESC LIMIT 10")
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
        button["starred"] = bool(button["starred"])
        button["stats"] = stats.get(button["id"], {"press_count": 0, "last_pressed": None})
    for node in nodes:
        node["enabled"] = bool(node["enabled"])
        node["health"] = node_health_cache.get(
            node["id"],
            {"status": "unknown", "checked_at": None, "latency_ms": None},
        )
    for schedule in schedules:
        schedule["enabled"] = bool(schedule["enabled"])
        schedule["days"] = [int(day) for day in schedule["days"].split(",") if day != ""]
    for timer in timer_presets:
        timer["active"] = bool(timer["active"])
        if timer["button_id"] is not None:
            timer["target_kind"] = "signal"
            timer["target_id"] = timer["button_id"]
        else:
            timer["target_kind"] = "workflow"
            timer["target_id"] = timer["workflow_id"]
    for schedule in workflow_schedules:
        schedule["enabled"] = bool(schedule["enabled"])
        schedule["days"] = [int(day) for day in schedule["days"].split(",") if day != ""]
    for workflow in workflows:
        workflow["starred"] = bool(workflow["starred"])
    for controller in ac_controllers:
        controller["swing"] = bool(controller["swing"])
    return {
        "nodes": nodes,
        "devices": devices,
        "buttons": buttons,
        "ac_controllers": ac_controllers,
        "timers": timers,
        "timer_presets": timer_presets,
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
        """
        INSERT INTO nodes (name, room, base_url, enabled, sort_order)
        VALUES (?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM nodes))
        """,
        (node.name.strip(), node.room.strip(), clean_base_url(node.base_url), int(node.enabled)),
    )
    if node.name.strip().lower() == "bedroom":
        db.execute("INSERT OR IGNORE INTO ac_controllers (node_id) VALUES (?)", (node_id,))
    return {"id": node_id}


@app.put("/api/nodes/order")
def reorder_nodes(order: NodeOrderIn) -> dict[str, Any]:
    current_ids = {node["id"] for node in db.fetch_all("SELECT id FROM nodes")}
    requested_ids = order.node_ids
    if len(requested_ids) != len(set(requested_ids)) or set(requested_ids) != current_ids:
        raise HTTPException(status_code=422, detail="node_ids must contain every node exactly once")
    with db.connect() as conn:
        conn.executemany(
            "UPDATE nodes SET sort_order = ? WHERE id = ?",
            [(index, node_id) for index, node_id in enumerate(requested_ids)],
        )
    return {"ok": True}


@app.put("/api/nodes/{node_id}")
def update_node(node_id: int, node: NodeUpdateIn) -> dict[str, Any]:
    current = db.fetch_one("SELECT base_url FROM nodes WHERE id = ?", (node_id,))
    if not current:
        raise HTTPException(status_code=404, detail="Node not found")
    name = node.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Node name is required")
    base_url = clean_base_url(node.base_url) if node.base_url is not None else current["base_url"]
    db.execute("UPDATE nodes SET name = ?, base_url = ? WHERE id = ?", (name, base_url, node_id))
    return {"id": node_id, "name": name, "base_url": base_url}


@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: int) -> dict[str, Any]:
    if not db.fetch_one("SELECT id FROM nodes WHERE id = ?", (node_id,)):
        raise HTTPException(status_code=404, detail="Node not found")
    workflows = db.fetch_all(
        """
        SELECT DISTINCT workflows.name
        FROM workflow_steps
        JOIN workflows ON workflows.id = workflow_steps.workflow_id
        JOIN buttons ON buttons.id = workflow_steps.button_id
        JOIN devices ON devices.id = buttons.device_id
        WHERE devices.node_id = ?
        ORDER BY workflows.name
        """,
        (node_id,),
    )
    if workflows:
        names = ", ".join(workflow["name"] for workflow in workflows)
        raise HTTPException(
            status_code=409,
            detail=f"Node signals are used by workflow: {names}. Edit the workflow before deleting the node.",
        )
    active_run = db.fetch_one(
        """
        SELECT workflow_runs.name
        FROM workflow_run_steps
        JOIN workflow_runs ON workflow_runs.id = workflow_run_steps.run_id
        JOIN buttons ON buttons.id = workflow_run_steps.button_id
        JOIN devices ON devices.id = buttons.device_id
        WHERE devices.node_id = ? AND workflow_runs.status IN ('pending', 'running')
        LIMIT 1
        """,
        (node_id,),
    )
    if active_run:
        raise HTTPException(
            status_code=409,
            detail=f"Node is used by active workflow run: {active_run['name']}. Cancel it before deleting the node.",
        )
    db.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
    return {"ok": True}


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


@app.post("/api/node-health/refresh")
async def refresh_all_node_health() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=NODE_HEALTH_TIMEOUT_SECONDS) as client:
        await refresh_node_health(client)
    online = sum(health["status"] == "online" for health in node_health_cache.values())
    return {"ok": True, "online": online, "total": len(node_health_cache)}


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


@app.post("/api/ac-controllers/{controller_id}/send")
async def send_ac_controller(controller_id: int, command: AcControllerCommandIn) -> dict[str, Any]:
    try:
        return await send_ac_controller_command(controller_id, command)
    except CommandError as exc:
        db.execute(
            "INSERT INTO events (button_id, status, message) VALUES (NULL, ?, ?)",
            ("failed", f"Aircond controller failed: {exc}"),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def capture_from_node(
    node_id: int,
    signal_type: Literal["rf", "ir"],
    timeout_ms: int,
    cancel_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    node = db.fetch_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        async with httpx.AsyncClient(timeout=(timeout_ms / 1000) + 4) as client:
            request_task = asyncio.create_task(
                client.get(
                    f"{clean_base_url(node['base_url'])}/capture/{signal_type}",
                    params={"timeout_ms": timeout_ms},
                )
            )
            cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event else None
            if cancel_task:
                done, _ = await asyncio.wait(
                    {request_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    request_task.cancel()
                    await asyncio.gather(request_task, return_exceptions=True)
                    raise CaptureCancelled("Capture cancelled")
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
            response = await request_task
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


async def capture_with_cancellation(
    node_id: int,
    signal_type: Literal["rf", "ir"],
    timeout_ms: int,
) -> dict[str, Any]:
    if node_id in active_capture_cancellations:
        raise HTTPException(status_code=409, detail="A capture is already running for this node")
    cancel_event = asyncio.Event()
    active_capture_cancellations[node_id] = cancel_event
    try:
        return await capture_from_node(node_id, signal_type, timeout_ms, cancel_event)
    except CaptureCancelled as exc:
        raise HTTPException(status_code=409, detail="Capture cancelled") from exc
    finally:
        active_capture_cancellations.pop(node_id, None)


@app.post("/api/nodes/{node_id}/cancel-capture")
async def cancel_capture(node_id: int) -> dict[str, Any]:
    cancel_event = active_capture_cancellations.get(node_id)
    if not cancel_event:
        return {"ok": True, "active": False}
    cancel_event.set()
    return {"ok": True, "active": True}


@app.post("/api/nodes/{node_id}/capture/{signal_type}")
async def capture_signal(
    node_id: int,
    signal_type: Literal["rf", "ir"],
    timeout_ms: int = Query(default=8000, ge=1000, le=30000),
) -> dict[str, Any]:
    normalized = await capture_with_cancellation(node_id, signal_type, timeout_ms)
    duplicate = find_duplicate_signal(node_id, signal_type, normalized)
    return {"payload": normalized, "duplicate": bool(duplicate), "existing": duplicate}


@app.post("/api/signals/save")
def save_captured_signal(signal: CapturedSignalIn) -> dict[str, Any]:
    if signal.signal_type == "rf":
        normalized = validate_rf_payload(signal.payload)
    else:
        raw, khz, repeat = validate_ir_payload(signal.payload)
        normalized = {"raw": raw, "khz": khz, "repeat": repeat}

    duplicate = find_duplicate_signal(signal.node_id, signal.signal_type, normalized)
    if duplicate:
        return {"duplicate": True, "existing": duplicate}

    device_id = ensure_signal_device(signal.node_id)
    name = signal.name.strip()
    button_id = db.execute(
        "INSERT INTO buttons (device_id, name, signal_type, payload) VALUES (?, ?, ?, ?)",
        (device_id, name, signal.signal_type, json_dumps(normalized)),
    )
    return {"duplicate": False, "id": button_id, "name": name}


@app.post("/api/signals/learn")
async def learn_signal(signal: LearnSignalIn) -> dict[str, Any]:
    normalized = await capture_with_cancellation(signal.node_id, signal.signal_type, signal.timeout_ms)
    duplicate = find_duplicate_signal(signal.node_id, signal.signal_type, normalized)
    if duplicate:
        return {"duplicate": True, "existing": duplicate, "payload": normalized}

    device_id = ensure_signal_device(signal.node_id)
    button_id = db.execute(
        "INSERT INTO buttons (device_id, name, signal_type, payload) VALUES (?, ?, ?, ?)",
        (device_id, signal.name.strip(), signal.signal_type, json_dumps(normalized)),
    )
    return {"duplicate": False, "id": button_id, "name": signal.name.strip(), "payload": normalized}


@app.put("/api/signals/{signal_id}")
def update_signal(signal_id: int, signal: SignalUpdateIn) -> dict[str, Any]:
    ensure_button_exists(signal_id)
    name = signal.name.strip()
    db.execute("UPDATE buttons SET name = ? WHERE id = ?", (name, signal_id))
    return {"id": signal_id, "name": name}


@app.delete("/api/signals/{signal_id}")
def delete_signal(signal_id: int) -> dict[str, Any]:
    ensure_button_exists(signal_id)
    workflows = db.fetch_all(
        """
        SELECT DISTINCT workflows.name
        FROM workflow_steps
        JOIN workflows ON workflows.id = workflow_steps.workflow_id
        WHERE workflow_steps.button_id = ?
        ORDER BY workflows.name
        """,
        (signal_id,),
    )
    if workflows:
        names = ", ".join(workflow["name"] for workflow in workflows)
        raise HTTPException(
            status_code=409,
            detail=f"Signal is used by workflow: {names}. Edit the workflow before deleting it.",
        )
    db.execute("DELETE FROM buttons WHERE id = ?", (signal_id,))
    return {"ok": True}


@app.put("/api/actions/{action_type}/{action_id}/star")
def set_action_star(
    action_type: Literal["signal", "workflow"],
    action_id: int,
    star: StarIn,
) -> dict[str, Any]:
    table = "buttons" if action_type == "signal" else "workflows"
    if not db.fetch_one(f"SELECT id FROM {table} WHERE id = ?", (action_id,)):
        raise HTTPException(status_code=404, detail="Action not found")
    db.execute(
        f"UPDATE {table} SET starred = ? WHERE id = ?",
        (int(star.starred), action_id),
    )
    return {"ok": True, "starred": star.starred}


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


def ensure_timer_preset_target(timer: TimerPresetIn) -> None:
    if timer.target_kind == "signal":
        ensure_button_exists(timer.target_id)
    elif not db.fetch_one("SELECT id FROM workflows WHERE id = ?", (timer.target_id,)):
        raise HTTPException(status_code=404, detail="Workflow not found")


@app.post("/api/timer-presets")
def create_timer_preset(timer: TimerPresetIn) -> dict[str, Any]:
    ensure_timer_preset_target(timer)
    button_id = timer.target_id if timer.target_kind == "signal" else None
    workflow_id = timer.target_id if timer.target_kind == "workflow" else None
    timer_id = db.execute(
        """
        INSERT INTO timer_presets (button_id, workflow_id, duration_seconds)
        VALUES (?, ?, ?)
        """,
        (button_id, workflow_id, timer.seconds),
    )
    return {"id": timer_id}


@app.put("/api/timer-presets/{timer_id}")
def update_timer_preset(timer_id: int, timer: TimerPresetIn) -> dict[str, Any]:
    existing = db.fetch_one("SELECT active FROM timer_presets WHERE id = ?", (timer_id,))
    if not existing:
        raise HTTPException(status_code=404, detail="Timer not found")
    if existing["active"]:
        raise HTTPException(status_code=409, detail="Cancel timer before editing")
    ensure_timer_preset_target(timer)
    button_id = timer.target_id if timer.target_kind == "signal" else None
    workflow_id = timer.target_id if timer.target_kind == "workflow" else None
    db.execute(
        """
        UPDATE timer_presets
        SET button_id = ?, workflow_id = ?, duration_seconds = ?, error = NULL
        WHERE id = ?
        """,
        (button_id, workflow_id, timer.seconds, timer_id),
    )
    return {"ok": True}


@app.post("/api/timer-presets/{timer_id}/start")
def start_timer_preset(timer_id: int) -> dict[str, Any]:
    timer = db.fetch_one("SELECT * FROM timer_presets WHERE id = ?", (timer_id,))
    if not timer:
        raise HTTPException(status_code=404, detail="Timer not found")
    if timer["active"]:
        raise HTTPException(status_code=409, detail="Timer is already active")
    run_at = utc_stamp(utc_now() + timedelta(seconds=timer["duration_seconds"]))
    db.execute(
        "UPDATE timer_presets SET active = 1, run_at_utc = ?, error = NULL WHERE id = ?",
        (run_at, timer_id),
    )
    return {"ok": True, "run_at_utc": run_at}


@app.post("/api/timer-presets/{timer_id}/cancel")
def cancel_timer_preset(timer_id: int) -> dict[str, Any]:
    if not db.fetch_one("SELECT id FROM timer_presets WHERE id = ?", (timer_id,)):
        raise HTTPException(status_code=404, detail="Timer not found")
    db.execute(
        "UPDATE timer_presets SET active = 0, run_at_utc = NULL, error = NULL WHERE id = ?",
        (timer_id,),
    )
    return {"ok": True}


@app.delete("/api/timer-presets/{timer_id}")
def delete_timer_preset(timer_id: int) -> dict[str, Any]:
    if not db.fetch_one("SELECT id FROM timer_presets WHERE id = ?", (timer_id,)):
        raise HTTPException(status_code=404, detail="Timer not found")
    db.execute("DELETE FROM timer_presets WHERE id = ?", (timer_id,))
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


@app.delete("/api/workflows/{workflow_id}")
def delete_workflow(workflow_id: int) -> dict[str, Any]:
    workflow = db.fetch_one("SELECT id, name FROM workflows WHERE id = ?", (workflow_id,))
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    active_run = db.fetch_one(
        """
        SELECT id
        FROM workflow_runs
        WHERE workflow_id = ? AND status IN ('pending', 'running')
        LIMIT 1
        """,
        (workflow_id,),
    )
    if active_run:
        raise HTTPException(
            status_code=409,
            detail=f"Workflow {workflow['name']} has an active run. Cancel it before deleting the workflow.",
        )

    db.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
    return {"ok": True}


@app.post("/api/workflows/{workflow_id}/run")
def run_workflow(workflow_id: int, run: WorkflowRunIn | None = None) -> dict[str, Any]:
    run = run or WorkflowRunIn()
    return create_workflow_run_record(
        workflow_id,
        start_delay_seconds=run.delay_seconds,
        run_name=run.name,
    )


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


def schedule_storage(schedule_kind: Literal["signal", "workflow"]) -> tuple[str, str]:
    if schedule_kind == "signal":
        return "schedules", "button_id"
    return "workflow_schedules", "workflow_id"


def ensure_schedule_target(schedule: ScheduleItemIn) -> None:
    if schedule.target_kind == "signal":
        ensure_button_exists(schedule.target_id)
    elif not db.fetch_one("SELECT id FROM workflows WHERE id = ?", (schedule.target_id,)):
        raise HTTPException(status_code=404, detail="Workflow not found")


@app.post("/api/schedule-items")
def create_schedule_item(schedule: ScheduleItemIn) -> dict[str, Any]:
    ensure_schedule_target(schedule)
    time_of_day, unique_days = normalize_schedule(schedule.time_of_day, schedule.days)
    table, target_column = schedule_storage(schedule.target_kind)
    schedule_id = db.execute(
        f"""
        INSERT INTO {table} ({target_column}, name, time_of_day, days, enabled)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            schedule.target_id,
            schedule.name.strip(),
            time_of_day,
            ",".join(str(day) for day in unique_days),
            int(schedule.enabled),
        ),
    )
    return {"id": schedule_id, "kind": schedule.target_kind}


@app.put("/api/schedule-items/{schedule_kind}/{schedule_id}")
def update_schedule_item(
    schedule_kind: Literal["signal", "workflow"],
    schedule_id: int,
    schedule: ScheduleItemIn,
) -> dict[str, Any]:
    source_table, _ = schedule_storage(schedule_kind)
    if not db.fetch_one(f"SELECT id FROM {source_table} WHERE id = ?", (schedule_id,)):
        raise HTTPException(status_code=404, detail="Schedule not found")
    ensure_schedule_target(schedule)
    time_of_day, unique_days = normalize_schedule(schedule.time_of_day, schedule.days)
    target_table, target_column = schedule_storage(schedule.target_kind)
    values = (
        schedule.target_id,
        schedule.name.strip(),
        time_of_day,
        ",".join(str(day) for day in unique_days),
        int(schedule.enabled),
    )
    with db.connect() as conn:
        if source_table == target_table:
            conn.execute(
                f"""
                UPDATE {source_table}
                SET {target_column} = ?, name = ?, time_of_day = ?, days = ?,
                    enabled = ?, last_run_date = NULL
                WHERE id = ?
                """,
                (*values, schedule_id),
            )
            updated_id = schedule_id
        else:
            conn.execute(f"DELETE FROM {source_table} WHERE id = ?", (schedule_id,))
            cursor = conn.execute(
                f"""
                INSERT INTO {target_table} ({target_column}, name, time_of_day, days, enabled)
                VALUES (?, ?, ?, ?, ?)
                """,
                values,
            )
            updated_id = int(cursor.lastrowid)
    return {"id": updated_id, "kind": schedule.target_kind}


@app.put("/api/schedule-items/{schedule_kind}/{schedule_id}/enabled")
def set_schedule_enabled(
    schedule_kind: Literal["signal", "workflow"],
    schedule_id: int,
    value: EnabledIn,
) -> dict[str, Any]:
    table, _ = schedule_storage(schedule_kind)
    if not db.fetch_one(f"SELECT id FROM {table} WHERE id = ?", (schedule_id,)):
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.execute("UPDATE " + table + " SET enabled = ? WHERE id = ?", (int(value.enabled), schedule_id))
    return {"ok": True, "enabled": value.enabled}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
