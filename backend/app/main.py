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
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db

APP_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Kuala_Lumpur"))
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
NODE_HEALTH_INTERVAL_SECONDS = 15
NODE_HEALTH_TIMEOUT_SECONDS = 3
NODE_HEALTH_FAILURE_THRESHOLD = 3
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
    node_id: int | None = Field(default=None, gt=0)
    temperature: int = Field(ge=16, le=30)
    fan: Literal["auto", "1", "2", "3"]
    swing: bool
    power_toggle: bool = False


class AcControllerIn(BaseModel):
    name: str = Field(default="Aircond controller", min_length=1, max_length=80)
    protocol: Literal["daikin64", "panasonic_ac"]
    last_node_id: int | None = Field(default=None, gt=0)
    temperature: int = Field(default=26, ge=16, le=30)
    fan: Literal["auto", "1", "2", "3"] = "auto"
    swing: bool = True


class AcControllerUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


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


def controller_slug_base(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "controller"


def controller_navigation() -> list[dict[str, Any]]:
    controllers = db.find_all("ac_controllers", sort=[("id", 1)])
    bases = [controller_slug_base(controller["name"]) for controller in controllers]
    counts = {base: bases.count(base) for base in set(bases)}
    for controller, base in zip(controllers, bases):
        controller["slug"] = base if counts[base] == 1 else f"{base}-{controller['id']}"
    return controllers


def controller_details(controller_id: int) -> dict[str, Any] | None:
    controller = db.find_one("ac_controllers", {"id": controller_id})
    if not controller:
        return None
    normalize_controller_metadata(controller)
    controller["swing"] = bool(controller["swing"])
    last_node_id = controller.get("last_node_id") or controller.pop("node_id", None)
    controller["last_node_id"] = last_node_id
    return controller


def controller_node_options() -> list[dict[str, Any]]:
    nodes = db.find_all("nodes", sort=[("sort_order", 1), ("id", 1)])
    for node in nodes:
        node["enabled"] = bool(node["enabled"])
        node["health"] = node_health_cache.get(
            node["id"],
            {"status": "unknown", "checked_at": None, "latency_ms": None},
        )
    return nodes


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
    return {"raw": raw, "khz": 38, "repeat": 1, "state": state, "protocol": "daikin64"}


PANASONIC_AC_BASE_STATE = (
    0x02, 0x20, 0xE0, 0x04, 0x00, 0x00, 0x00, 0x06,
    0x02, 0x20, 0xE0, 0x04, 0x00, 0x3C, 0x34, 0x80,
    0xAF, 0x00, 0x00, 0x0E, 0xE0, 0x00, 0x00, 0x89,
    0x00, 0x00, 0x00,
)
AC_PROTOCOL_BRANDS = {
    "daikin64": "Daikin",
    "panasonic_ac": "Panasonic",
}


def normalize_controller_metadata(controller: dict[str, Any]) -> None:
    protocol = str(controller.get("protocol") or "daikin64")
    controller["protocol"] = protocol
    controller["brand"] = str(
        controller.get("brand") or AC_PROTOCOL_BRANDS.get(protocol, "Unknown")
    )
    controller["power"] = bool(controller.get("power", 0))


def panasonic_ac_payload(command: AcControllerCommandIn, power: bool) -> dict[str, Any]:
    state = list(PANASONIC_AC_BASE_STATE)
    fan_codes = {"auto": 0xA0, "1": 0x30, "2": 0x50, "3": 0x70}

    state[13] = 0x3D if power else 0x3C  # Cool mode with explicit power state.
    state[14] = command.temperature << 1
    state[16] = fan_codes[command.fan] | (0x0F if command.swing else 0x03)
    state[26] = (0xF4 + sum(state[:26])) & 0xFF

    raw: list[int] = []
    for section_index, section in enumerate((state[:8], state[8:])):
        raw.extend((3456, 1728))
        for byte in section:
            for bit in range(8):
                raw.extend((432, 1296 if byte & (1 << bit) else 432))
        raw.append(432)
        if section_index == 0:
            raw.append(10000)

    return {
        "raw": raw,
        "khz": 37,
        "repeat": 1,
        "state": state,
        "protocol": "panasonic_ac",
    }


def build_ac_controller_payload(
    controller: dict[str, Any],
    command: AcControllerCommandIn,
    power: bool,
) -> dict[str, Any]:
    protocol = str(controller.get("protocol") or "daikin64")
    builders = {
        "daikin64": lambda: daikin64_payload(command),
        "panasonic_ac": lambda: panasonic_ac_payload(command, power),
    }
    try:
        return builders[protocol]()
    except KeyError as exc:
        raise CommandError(f"Unsupported aircond protocol: {protocol}") from exc


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
    row = db.find_one("buttons", {"id": button_id})
    if not row:
        raise HTTPException(status_code=404, detail="Button not found")
    device = db.find_one("devices", {"id": row["device_id"]})
    node = db.find_one("nodes", {"id": device["node_id"]}) if device else None
    if not device or not node:
        raise HTTPException(status_code=404, detail="Button node not found")
    row.update(
        device_name=device["name"],
        device_kind=device["kind"],
        node_base_url=node["base_url"],
        node_enabled=node["enabled"],
    )
    return row


def ensure_signal_device(node_id: int) -> int:
    node = db.find_one("nodes", {"id": node_id})
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    device = db.find_one("devices", {"node_id": node_id, "name": "Signals"})
    if device:
        return int(device["id"])

    return db.insert(
        "devices",
        {
            "node_id": node_id,
            "name": "Signals",
            "room": node["room"],
            "kind": "other",
            "notes": "Auto-created for learned signals",
        },
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
    devices = db.find_all("devices", {"node_id": node_id})
    device_by_id = {device["id"]: device for device in devices}
    node = db.find_one("nodes", {"id": node_id})
    buttons = db.find_all(
        "buttons",
        {"device_id": {"$in": list(device_by_id)}, "signal_type": signal_type},
        sort=[("created_at", 1), ("id", 1)],
    )
    for button in buttons:
        if signal_payload_matches(signal_type, json_loads(button["payload"]), payload):
            device = device_by_id[button["device_id"]]
            return {
                "id": button["id"],
                "name": button["name"],
                "device_name": device["name"],
                "node_name": node["name"] if node else "Unknown",
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
    db.insert("events", {"button_id": button_id, "status": "sent", "message": message})
    return {"ok": True, "message": message}


async def send_ac_controller_command(
    controller_id: int,
    command: AcControllerCommandIn,
) -> dict[str, Any]:
    controller = db.find_one("ac_controllers", {"id": controller_id})
    if not controller:
        raise HTTPException(status_code=404, detail="Aircond controller not found")
    normalize_controller_metadata(controller)
    node_id = command.node_id or controller.get("last_node_id") or controller.get("node_id")
    if not node_id:
        raise CommandError("Choose a node")
    node = db.find_one("nodes", {"id": node_id})
    if not node:
        raise CommandError("Selected node not found")
    if not node["enabled"]:
        raise CommandError("Node is disabled")

    power = not controller["power"] if command.power_toggle else controller["power"]
    payload = build_ac_controller_payload(controller, command, power)
    raw, khz, repeat = validate_ir_payload(payload)
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                f"{clean_base_url(node['base_url'])}/send/ir/raw",
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
        f"Swing {'auto' if command.swing else 'fixed'}"
    )
    last_command = f"Power {'ON' if power else 'OFF'} - {settings}"
    sent_at = utc_stamp()
    db.update(
        "ac_controllers",
        {"id": controller_id},
        {
            "temperature": command.temperature,
            "fan": command.fan,
            "swing": int(command.swing),
            "power": int(power),
            "last_node_id": node["id"],
            "last_command": last_command,
            "last_sent_at": sent_at,
        },
    )
    message = f"Aircond controller sent {last_command} to {node['name']}"
    db.insert("events", {"button_id": None, "status": "sent", "message": message})
    updated = controller_details(controller_id)
    return {"ok": True, "message": message, "controller": updated}


def create_workflow_run_record(
    workflow_id: int,
    *,
    start_delay_seconds: int = 0,
    run_name: str | None = None,
) -> dict[str, Any]:
    workflow = db.find_one("workflows", {"id": workflow_id})
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    steps = db.find_all("workflow_steps", {"workflow_id": workflow_id}, sort=[("step_order", 1)])
    if not steps:
        raise HTTPException(status_code=422, detail="Workflow has no steps")

    first_run_at = utc_stamp(
        utc_now() + timedelta(seconds=start_delay_seconds + steps[0]["delay_seconds"])
    )
    name = run_name.strip() if run_name else workflow["name"]
    run_id = db.insert(
        "workflow_runs",
        {
            "workflow_id": workflow_id,
            "name": name,
            "status": "pending",
            "error": None,
            "started_at_utc": None,
            "finished_at_utc": None,
        },
    )
    db.insert_many(
        "workflow_run_steps",
        [
            {
                "run_id": run_id,
                "workflow_step_id": step["id"],
                "step_order": step["step_order"],
                "button_id": step["button_id"],
                "delay_seconds": step["delay_seconds"],
                "run_after_utc": first_run_at if index == 0 else None,
                "status": "pending" if index == 0 else "waiting",
                "error": None,
                "fired_at_utc": None,
            }
            for index, step in enumerate(steps)
        ],
    )
    return {"id": run_id, "first_run_at_utc": first_run_at, "name": name}


async def run_due_timers() -> None:
    due = db.find_all(
        "timers",
        {"status": "pending", "run_at_utc": {"$lte": utc_stamp()}},
        sort=[("run_at_utc", 1)],
        limit=10,
    )
    for timer in due:
        if not db.update("timers", {"id": timer["id"], "status": "pending"}, {"status": "running"}):
            continue
        try:
            await send_button_to_node(timer["button_id"])
            db.update(
                "timers",
                {"id": timer["id"]},
                {"status": "done", "fired_at_utc": utc_stamp(), "error": None},
            )
        except Exception as exc:
            message = str(exc)
            db.update(
                "timers",
                {"id": timer["id"]},
                {"status": "failed", "fired_at_utc": utc_stamp(), "error": message},
            )
            db.insert("events", {"button_id": timer["button_id"], "status": "failed", "message": message})


async def run_due_timer_presets() -> None:
    due = db.find_all(
        "timer_presets",
        {"active": 1, "run_at_utc": {"$lte": utc_stamp()}},
        sort=[("run_at_utc", 1)],
        limit=10,
    )
    for timer in due:
        if not db.update(
            "timer_presets", {"id": timer["id"], "active": 1}, {"active": 0, "run_at_utc": None}
        ):
            continue
        try:
            if timer["button_id"] is not None:
                await send_button_to_node(timer["button_id"])
            else:
                run = create_workflow_run_record(timer["workflow_id"])
                db.insert(
                    "events",
                    {"button_id": None, "status": "workflow", "message": f"Started workflow {run['name']} from timer"},
                )
            db.update("timer_presets", {"id": timer["id"]}, {"error": None})
        except Exception as exc:
            message = str(exc)
            db.update("timer_presets", {"id": timer["id"]}, {"error": message})
            db.insert(
                "events",
                {"button_id": timer["button_id"], "status": "failed", "message": f"Timer failed: {message}"},
            )


async def run_due_schedules() -> None:
    now = local_now()
    current_time = now.strftime("%H:%M")
    today = now.date().isoformat()
    weekday = now.weekday()
    schedules = db.find_all("schedules", {"enabled": 1, "time_of_day": current_time})
    for schedule in schedules:
        days = {int(day) for day in schedule["days"].split(",") if day != ""}
        if weekday not in days or schedule["last_run_date"] == today:
            continue
        db.update("schedules", {"id": schedule["id"]}, {"last_run_date": today})
        try:
            await send_button_to_node(schedule["button_id"])
        except Exception as exc:
            db.insert("events", {"button_id": schedule["button_id"], "status": "failed", "message": str(exc)})


async def run_due_workflow_schedules() -> None:
    now = local_now()
    current_time = now.strftime("%H:%M")
    today = now.date().isoformat()
    weekday = now.weekday()
    schedules = db.find_all("workflow_schedules", {"enabled": 1, "time_of_day": current_time})
    for schedule in schedules:
        days = {int(day) for day in schedule["days"].split(",") if day != ""}
        if weekday not in days or schedule["last_run_date"] == today:
            continue
        db.update("workflow_schedules", {"id": schedule["id"]}, {"last_run_date": today})
        try:
            run = create_workflow_run_record(schedule["workflow_id"])
            db.insert(
                "events",
                {"button_id": None, "status": "workflow", "message": f"Started workflow {run['name']} from schedule {schedule['name']}"},
            )
        except Exception as exc:
            db.insert(
                "events",
                {"button_id": None, "status": "failed", "message": f"Workflow schedule {schedule['name']} failed: {exc}"},
            )


async def run_due_workflow_steps() -> None:
    due = db.find_all(
        "workflow_run_steps",
        {"status": "pending", "run_after_utc": {"$lte": utc_stamp()}},
        sort=[("run_after_utc", 1)],
        limit=5,
    )

    for step in due:
        run = db.find_one("workflow_runs", {"id": step["run_id"], "status": {"$in": ["pending", "running"]}})
        if not run or not db.update(
            "workflow_run_steps", {"id": step["id"], "status": "pending"}, {"status": "running"}
        ):
            continue
        db.update(
            "workflow_runs",
            {"id": step["run_id"], "status": {"$in": ["pending", "running"]}},
            {"status": "running", "started_at_utc": run.get("started_at_utc") or utc_stamp()},
        )
        step["run_name"] = run["name"]

        try:
            await send_button_to_node(step["button_id"])
            db.update(
                "workflow_run_steps",
                {"id": step["id"]},
                {"status": "done", "fired_at_utc": utc_stamp(), "error": None},
            )

            next_step = db.find_one(
                "workflow_run_steps",
                {"run_id": step["run_id"], "step_order": step["step_order"] + 1},
            )
            if next_step:
                db.update(
                    "workflow_run_steps",
                    {"id": next_step["id"], "status": "waiting"},
                    {"status": "pending", "run_after_utc": utc_stamp(utc_now() + timedelta(seconds=next_step["delay_seconds"]))},
                )
            else:
                db.update(
                    "workflow_runs",
                    {"id": step["run_id"]},
                    {"status": "done", "finished_at_utc": utc_stamp(), "error": None},
                )
        except Exception as exc:
            message = str(exc)
            db.update(
                "workflow_run_steps",
                {"id": step["id"]},
                {"status": "failed", "fired_at_utc": utc_stamp(), "error": message},
            )
            db.update(
                "workflow_run_steps",
                {"run_id": step["run_id"], "status": {"$in": ["waiting", "pending"]}},
                {"status": "cancelled"},
                many=True,
            )
            db.update(
                "workflow_runs",
                {"id": step["run_id"]},
                {"status": "failed", "finished_at_utc": utc_stamp(), "error": message},
            )
            db.insert(
                "events",
                {"button_id": step["button_id"], "status": "failed", "message": f"Workflow {step['run_name']} failed: {message}"},
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
        return {
            "status": "disabled",
            "checked_at": checked_at,
            "latency_ms": None,
            "failures": 0,
        }

    previous = node_health_cache.get(node["id"], {})
    if node["id"] in active_capture_cancellations:
        return {
            **previous,
            "status": previous.get("status", "unknown"),
            "busy": "capture",
        }

    started_at = asyncio.get_running_loop().time()
    try:
        response = await client.get(f"{clean_base_url(node['base_url'])}/health")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        failures = int(previous.get("failures", 0)) + 1
        status = "offline" if failures >= NODE_HEALTH_FAILURE_THRESHOLD else previous.get(
            "status", "unknown"
        )
        return {
            "status": status,
            "checked_at": checked_at,
            "latency_ms": None,
            "error": str(exc) or type(exc).__name__,
            "failures": failures,
        }

    latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
    return {
        "status": "online",
        "checked_at": checked_at,
        "latency_ms": latency_ms,
        "failures": 0,
    }


async def refresh_node_health(client: httpx.AsyncClient) -> None:
    global node_health_cache
    nodes = db.find_all("nodes", sort=[("id", 1)])
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
        db.close()


app = FastAPI(title="Local RF/IR Controller", lifespan=lifespan)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{controller_id:int}")
def legacy_controller_page(controller_id: int) -> RedirectResponse:
    controller = next(
        (item for item in controller_navigation() if item["id"] == controller_id),
        None,
    )
    if not controller:
        raise HTTPException(status_code=404, detail="Aircond controller not found")
    return RedirectResponse(url=f"/{controller['slug']}", status_code=307)


@app.get("/{controller_slug}")
def controller_page(controller_slug: str) -> FileResponse:
    if not any(item["slug"] == controller_slug for item in controller_navigation()):
        raise HTTPException(status_code=404, detail="Aircond controller not found")
    return FileResponse(STATIC_DIR / "controller.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time_utc": utc_stamp(), "timezone": str(APP_TIMEZONE)}


@app.get("/api/events")
def events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    total = db.count("events")
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    items = db.find_all(
        "events",
        sort=[("created_at", -1), ("id", -1)],
        limit=page_size,
        skip=(current_page - 1) * page_size,
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
    nodes = db.find_all("nodes", sort=[("sort_order", 1), ("id", 1)])
    devices = db.find_all("devices", sort=[("room", 1), ("name", 1)])
    buttons = db.find_all("buttons", sort=[("name", 1)])
    ac_controllers = db.find_all("ac_controllers", sort=[("id", 1)])
    timers = db.find_all("timers", sort=[("created_at", -1)], limit=50)
    timer_presets = db.find_all("timer_presets", sort=[("created_at", -1)])
    schedules = db.find_all("schedules", sort=[("time_of_day", 1), ("name", 1)])
    workflows = db.find_all("workflows", sort=[("created_at", -1)])
    workflow_steps = db.find_all("workflow_steps", sort=[("workflow_id", 1), ("step_order", 1)])
    workflow_schedules = db.find_all("workflow_schedules", sort=[("time_of_day", 1), ("name", 1)])
    workflow_runs = db.find_all("workflow_runs", sort=[("created_at", -1)], limit=30)
    workflow_run_steps = db.find_all("workflow_run_steps", sort=[("run_id", 1), ("step_order", 1)])
    events = db.find_all("events", sort=[("created_at", -1), ("id", -1)], limit=10)
    stats = {
        row["button_id"]: row
        for row in db.aggregate(
            "events",
            [
                {"$match": {"status": "sent", "button_id": {"$ne": None}}},
                {"$group": {"_id": "$button_id", "press_count": {"$sum": 1}, "last_pressed": {"$max": "$created_at"}}},
                {"$project": {"_id": 0, "button_id": "$_id", "press_count": 1, "last_pressed": 1}},
            ],
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
        normalize_controller_metadata(controller)
        controller["swing"] = bool(controller["swing"])
        controller["last_node_id"] = controller.get("last_node_id") or controller.pop("node_id", None)
    controller_slugs = {item["id"]: item["slug"] for item in controller_navigation()}
    for controller in ac_controllers:
        controller["slug"] = controller_slugs[controller["id"]]
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
    last_node = db.find_one("nodes", sort=[("sort_order", -1)])
    node_id = db.insert(
        "nodes",
        {
            "name": node.name.strip(),
            "room": node.room.strip(),
            "base_url": clean_base_url(node.base_url),
            "enabled": int(node.enabled),
            "sort_order": (last_node["sort_order"] + 1) if last_node else 0,
            "last_seen": None,
        },
    )
    return {"id": node_id}


@app.put("/api/nodes/order")
def reorder_nodes(order: NodeOrderIn) -> dict[str, Any]:
    current_ids = {node["id"] for node in db.find_all("nodes")}
    requested_ids = order.node_ids
    if len(requested_ids) != len(set(requested_ids)) or set(requested_ids) != current_ids:
        raise HTTPException(status_code=422, detail="node_ids must contain every node exactly once")
    for index, node_id in enumerate(requested_ids):
        db.update("nodes", {"id": node_id}, {"sort_order": index})
    return {"ok": True}


@app.put("/api/nodes/{node_id}")
def update_node(node_id: int, node: NodeUpdateIn) -> dict[str, Any]:
    current = db.find_one("nodes", {"id": node_id})
    if not current:
        raise HTTPException(status_code=404, detail="Node not found")
    name = node.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Node name is required")
    base_url = clean_base_url(node.base_url) if node.base_url is not None else current["base_url"]
    db.update("nodes", {"id": node_id}, {"name": name, "base_url": base_url})
    return {"id": node_id, "name": name, "base_url": base_url}


@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: int) -> dict[str, Any]:
    if not db.find_one("nodes", {"id": node_id}):
        raise HTTPException(status_code=404, detail="Node not found")
    device_ids = db.distinct("devices", "id", {"node_id": node_id})
    button_ids = db.distinct("buttons", "id", {"device_id": {"$in": device_ids}}) if device_ids else []
    workflow_ids = db.distinct("workflow_steps", "workflow_id", {"button_id": {"$in": button_ids}}) if button_ids else []
    workflows = db.find_all("workflows", {"id": {"$in": workflow_ids}}, sort=[("name", 1)]) if workflow_ids else []
    if workflows:
        names = ", ".join(workflow["name"] for workflow in workflows)
        raise HTTPException(
            status_code=409,
            detail=f"Node signals are used by workflow: {names}. Edit the workflow before deleting the node.",
        )
    run_ids = db.distinct("workflow_run_steps", "run_id", {"button_id": {"$in": button_ids}}) if button_ids else []
    active_run = db.find_one(
        "workflow_runs",
        {"id": {"$in": run_ids}, "status": {"$in": ["pending", "running"]}},
    ) if run_ids else None
    if active_run:
        raise HTTPException(
            status_code=409,
            detail=f"Node is used by active workflow run: {active_run['name']}. Cancel it before deleting the node.",
        )
    if button_ids:
        for entity in ("timers", "schedules", "timer_presets", "workflow_run_steps"):
            db.delete(entity, {"button_id": {"$in": button_ids}}, many=True)
        db.update("events", {"button_id": {"$in": button_ids}}, {"button_id": None}, many=True)
        db.delete("buttons", {"id": {"$in": button_ids}}, many=True)
    if device_ids:
        db.delete("devices", {"id": {"$in": device_ids}}, many=True)
    db.update("ac_controllers", {"last_node_id": node_id}, {"last_node_id": None}, many=True)
    db.delete("nodes", {"id": node_id})
    return {"ok": True}


@app.post("/api/nodes/{node_id}/ping")
async def ping_node(node_id: int) -> dict[str, Any]:
    node = db.find_one("nodes", {"id": node_id})
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{clean_base_url(node['base_url'])}/health")
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Node ping failed: {exc}") from exc
    db.update("nodes", {"id": node_id}, {"last_seen": utc_stamp()})
    return {"ok": True, "node": data}


@app.post("/api/node-health/refresh")
async def refresh_all_node_health() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=NODE_HEALTH_TIMEOUT_SECONDS) as client:
        await refresh_node_health(client)
    online = sum(health["status"] == "online" for health in node_health_cache.values())
    return {"ok": True, "online": online, "total": len(node_health_cache)}


@app.post("/api/devices")
def create_device(device: DeviceIn) -> dict[str, Any]:
    if not db.find_one("nodes", {"id": device.node_id}):
        raise HTTPException(status_code=404, detail="Node not found")
    device_id = db.insert(
        "devices",
        {
            "node_id": device.node_id,
            "name": device.name.strip(),
            "room": device.room.strip(),
            "kind": device.kind,
            "notes": device.notes.strip(),
        },
    )
    return {"id": device_id}


@app.post("/api/buttons")
def create_button(button: ButtonIn) -> dict[str, Any]:
    if not db.find_one("devices", {"id": button.device_id}):
        raise HTTPException(status_code=404, detail="Device not found")
    if button.signal_type == "rf":
        validate_rf_payload(button.payload)
    else:
        validate_ir_payload(button.payload)
    button_id = db.insert(
        "buttons",
        {
            "device_id": button.device_id,
            "name": button.name.strip(),
            "signal_type": button.signal_type,
            "payload": json_dumps(button.payload),
            "starred": 0,
        },
    )
    return {"id": button_id}


@app.post("/api/buttons/{button_id}/press")
async def press_button(button_id: int) -> dict[str, Any]:
    try:
        return await send_button_to_node(button_id)
    except CommandError as exc:
        db.insert("events", {"button_id": button_id, "status": "failed", "message": str(exc)})
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/controller-pages/{controller_slug}")
def get_ac_controller_page(controller_slug: str) -> dict[str, Any]:
    controllers = controller_navigation()
    selected = next((item for item in controllers if item["slug"] == controller_slug), None)
    if not selected:
        raise HTTPException(status_code=404, detail="Aircond controller not found")
    controller = controller_details(selected["id"])
    if not controller:
        raise HTTPException(status_code=404, detail="Aircond controller not found")
    controller["slug"] = selected["slug"]
    return {
        "controller": controller,
        "controllers": controllers,
        "nodes": controller_node_options(),
        "timezone": str(APP_TIMEZONE),
    }


@app.get("/api/ac-controllers/{controller_id}")
def get_ac_controller(controller_id: int) -> dict[str, Any]:
    controller = controller_details(controller_id)
    if not controller:
        raise HTTPException(status_code=404, detail="Aircond controller not found")
    controllers = controller_navigation()
    controller["slug"] = next(item["slug"] for item in controllers if item["id"] == controller_id)
    return {
        "controller": controller,
        "controllers": controllers,
        "nodes": controller_node_options(),
        "timezone": str(APP_TIMEZONE),
    }


@app.post("/api/ac-controllers")
def create_ac_controller(controller: AcControllerIn) -> dict[str, Any]:
    if controller.last_node_id and not db.find_one("nodes", {"id": controller.last_node_id}):
        raise HTTPException(status_code=404, detail="Last-used node not found")

    controller_id = db.insert(
        "ac_controllers",
        {
            "name": controller.name.strip(),
            "brand": AC_PROTOCOL_BRANDS[controller.protocol],
            "protocol": controller.protocol,
            "last_node_id": controller.last_node_id,
            "power": 0,
            "temperature": controller.temperature,
            "fan": controller.fan,
            "swing": int(controller.swing),
            "last_command": None,
            "last_sent_at": None,
        },
    )
    created = controller_details(controller_id)
    return {"id": controller_id, "controller": created}


@app.put("/api/ac-controllers/{controller_id}")
def update_ac_controller(controller_id: int, update: AcControllerUpdateIn) -> dict[str, Any]:
    if not db.find_one("ac_controllers", {"id": controller_id}):
        raise HTTPException(status_code=404, detail="Aircond controller not found")
    name = update.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Controller name is required")
    db.update("ac_controllers", {"id": controller_id}, {"name": name})
    controllers = controller_navigation()
    controller = next(item for item in controllers if item["id"] == controller_id)
    return {
        "id": controller_id,
        "name": name,
        "slug": controller["slug"],
        "controllers": controllers,
    }


@app.post("/api/ac-controllers/{controller_id}/send")
async def send_ac_controller(controller_id: int, command: AcControllerCommandIn) -> dict[str, Any]:
    try:
        return await send_ac_controller_command(controller_id, command)
    except CommandError as exc:
        db.insert(
            "events",
            {"button_id": None, "status": "failed", "message": f"Aircond controller failed: {exc}"},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def capture_from_node(
    node_id: int,
    signal_type: Literal["rf", "ir"],
    timeout_ms: int,
    cancel_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    node = db.find_one("nodes", {"id": node_id})
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
    db.update("nodes", {"id": node_id}, {"last_seen": utc_stamp()})
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
    button_id = db.insert(
        "buttons",
        {"device_id": device_id, "name": name, "signal_type": signal.signal_type, "payload": json_dumps(normalized), "starred": 0},
    )
    return {"duplicate": False, "id": button_id, "name": name}


@app.post("/api/signals/learn")
async def learn_signal(signal: LearnSignalIn) -> dict[str, Any]:
    normalized = await capture_with_cancellation(signal.node_id, signal.signal_type, signal.timeout_ms)
    duplicate = find_duplicate_signal(signal.node_id, signal.signal_type, normalized)
    if duplicate:
        return {"duplicate": True, "existing": duplicate, "payload": normalized}

    device_id = ensure_signal_device(signal.node_id)
    button_id = db.insert(
        "buttons",
        {"device_id": device_id, "name": signal.name.strip(), "signal_type": signal.signal_type, "payload": json_dumps(normalized), "starred": 0},
    )
    return {"duplicate": False, "id": button_id, "name": signal.name.strip(), "payload": normalized}


@app.put("/api/signals/{signal_id}")
def update_signal(signal_id: int, signal: SignalUpdateIn) -> dict[str, Any]:
    ensure_button_exists(signal_id)
    name = signal.name.strip()
    db.update("buttons", {"id": signal_id}, {"name": name})
    return {"id": signal_id, "name": name}


@app.delete("/api/signals/{signal_id}")
def delete_signal(signal_id: int) -> dict[str, Any]:
    ensure_button_exists(signal_id)
    workflow_ids = db.distinct("workflow_steps", "workflow_id", {"button_id": signal_id})
    workflows = db.find_all("workflows", {"id": {"$in": workflow_ids}}, sort=[("name", 1)]) if workflow_ids else []
    if workflows:
        names = ", ".join(workflow["name"] for workflow in workflows)
        raise HTTPException(
            status_code=409,
            detail=f"Signal is used by workflow: {names}. Edit the workflow before deleting it.",
        )
    for entity in ("timers", "schedules", "timer_presets", "workflow_run_steps"):
        db.delete(entity, {"button_id": signal_id}, many=True)
    db.update("events", {"button_id": signal_id}, {"button_id": None}, many=True)
    db.delete("buttons", {"id": signal_id})
    return {"ok": True}


@app.put("/api/actions/{action_type}/{action_id}/star")
def set_action_star(
    action_type: Literal["signal", "workflow"],
    action_id: int,
    star: StarIn,
) -> dict[str, Any]:
    table = "buttons" if action_type == "signal" else "workflows"
    if not db.find_one(table, {"id": action_id}):
        raise HTTPException(status_code=404, detail="Action not found")
    db.update(table, {"id": action_id}, {"starred": int(star.starred)})
    return {"ok": True, "starred": star.starred}


@app.post("/api/timers")
def create_timer(timer: TimerIn) -> dict[str, Any]:
    ensure_button_exists(timer.button_id)
    run_at = utc_stamp(utc_now() + timedelta(seconds=timer.seconds))
    timer_id = db.insert(
        "timers",
        {
            "button_id": timer.button_id,
            "name": timer.name.strip(),
            "run_at_utc": run_at,
            "status": "pending",
            "error": None,
            "fired_at_utc": None,
        },
    )
    return {"id": timer_id, "run_at_utc": run_at}


@app.post("/api/timers/{timer_id}/cancel")
def cancel_timer(timer_id: int) -> dict[str, Any]:
    db.update("timers", {"id": timer_id, "status": {"$in": ["pending", "running"]}}, {"status": "cancelled"})
    return {"ok": True}


def ensure_timer_preset_target(timer: TimerPresetIn) -> None:
    if timer.target_kind == "signal":
        ensure_button_exists(timer.target_id)
    elif not db.find_one("workflows", {"id": timer.target_id}):
        raise HTTPException(status_code=404, detail="Workflow not found")


@app.post("/api/timer-presets")
def create_timer_preset(timer: TimerPresetIn) -> dict[str, Any]:
    ensure_timer_preset_target(timer)
    button_id = timer.target_id if timer.target_kind == "signal" else None
    workflow_id = timer.target_id if timer.target_kind == "workflow" else None
    timer_id = db.insert(
        "timer_presets",
        {
            "button_id": button_id,
            "workflow_id": workflow_id,
            "duration_seconds": timer.seconds,
            "run_at_utc": None,
            "active": 0,
            "error": None,
            "legacy_timer_id": None,
        },
    )
    return {"id": timer_id}


@app.put("/api/timer-presets/{timer_id}")
def update_timer_preset(timer_id: int, timer: TimerPresetIn) -> dict[str, Any]:
    existing = db.find_one("timer_presets", {"id": timer_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Timer not found")
    if existing["active"]:
        raise HTTPException(status_code=409, detail="Cancel timer before editing")
    ensure_timer_preset_target(timer)
    button_id = timer.target_id if timer.target_kind == "signal" else None
    workflow_id = timer.target_id if timer.target_kind == "workflow" else None
    db.update(
        "timer_presets",
        {"id": timer_id},
        {"button_id": button_id, "workflow_id": workflow_id, "duration_seconds": timer.seconds, "error": None},
    )
    return {"ok": True}


@app.post("/api/timer-presets/{timer_id}/start")
def start_timer_preset(timer_id: int) -> dict[str, Any]:
    timer = db.find_one("timer_presets", {"id": timer_id})
    if not timer:
        raise HTTPException(status_code=404, detail="Timer not found")
    if timer["active"]:
        raise HTTPException(status_code=409, detail="Timer is already active")
    run_at = utc_stamp(utc_now() + timedelta(seconds=timer["duration_seconds"]))
    db.update("timer_presets", {"id": timer_id}, {"active": 1, "run_at_utc": run_at, "error": None})
    return {"ok": True, "run_at_utc": run_at}


@app.post("/api/timer-presets/{timer_id}/cancel")
def cancel_timer_preset(timer_id: int) -> dict[str, Any]:
    if not db.find_one("timer_presets", {"id": timer_id}):
        raise HTTPException(status_code=404, detail="Timer not found")
    db.update("timer_presets", {"id": timer_id}, {"active": 0, "run_at_utc": None, "error": None})
    return {"ok": True}


@app.delete("/api/timer-presets/{timer_id}")
def delete_timer_preset(timer_id: int) -> dict[str, Any]:
    if not db.find_one("timer_presets", {"id": timer_id}):
        raise HTTPException(status_code=404, detail="Timer not found")
    db.delete("timer_presets", {"id": timer_id})
    return {"ok": True}


@app.post("/api/workflows")
def create_workflow(workflow: WorkflowIn) -> dict[str, Any]:
    steps = workflow.steps
    for step in steps:
        ensure_button_exists(step.button_id)

    workflow_id = db.insert("workflows", {"name": workflow.name.strip(), "starred": 0})
    db.insert_many(
        "workflow_steps",
        [
            {
                "workflow_id": workflow_id,
                "step_order": index,
                "button_id": step.button_id,
                "delay_seconds": step.delay_seconds,
            }
            for index, step in enumerate(steps, start=1)
        ],
    )

    return {"id": workflow_id}


@app.put("/api/workflows/{workflow_id}")
def update_workflow(workflow_id: int, workflow: WorkflowIn) -> dict[str, Any]:
    if not db.find_one("workflows", {"id": workflow_id}):
        raise HTTPException(status_code=404, detail="Workflow not found")

    steps = workflow.steps
    for step in steps:
        ensure_button_exists(step.button_id)

    db.update("workflows", {"id": workflow_id}, {"name": workflow.name.strip()})
    db.delete("workflow_steps", {"workflow_id": workflow_id}, many=True)
    db.insert_many(
        "workflow_steps",
        [
            {
                "workflow_id": workflow_id,
                "step_order": index,
                "button_id": step.button_id,
                "delay_seconds": step.delay_seconds,
            }
            for index, step in enumerate(steps, start=1)
        ],
    )

    return {"id": workflow_id}


@app.delete("/api/workflows/{workflow_id}")
def delete_workflow(workflow_id: int) -> dict[str, Any]:
    workflow = db.find_one("workflows", {"id": workflow_id})
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    active_run = db.find_one(
        "workflow_runs",
        {"workflow_id": workflow_id, "status": {"$in": ["pending", "running"]}},
    )
    if active_run:
        raise HTTPException(
            status_code=409,
            detail=f"Workflow {workflow['name']} has an active run. Cancel it before deleting the workflow.",
        )

    run_ids = db.distinct("workflow_runs", "id", {"workflow_id": workflow_id})
    if run_ids:
        db.delete("workflow_run_steps", {"run_id": {"$in": run_ids}}, many=True)
    for entity in ("workflow_steps", "workflow_schedules", "timer_presets", "workflow_runs"):
        db.delete(entity, {"workflow_id": workflow_id}, many=True)
    db.delete("workflows", {"id": workflow_id})
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
    run = db.find_one("workflow_runs", {"id": run_id})
    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    db.update(
        "workflow_runs",
        {"id": run_id, "status": {"$in": ["pending", "running"]}},
        {"status": "cancelled", "finished_at_utc": utc_stamp()},
    )
    db.update(
        "workflow_run_steps",
        {"run_id": run_id, "status": {"$in": ["waiting", "pending", "running"]}},
        {"status": "cancelled"},
        many=True,
    )
    return {"ok": True}


@app.post("/api/workflow-schedules")
def create_workflow_schedule(schedule: WorkflowScheduleIn) -> dict[str, Any]:
    if not db.find_one("workflows", {"id": schedule.workflow_id}):
        raise HTTPException(status_code=404, detail="Workflow not found")
    time_of_day, unique_days = normalize_schedule(schedule.time_of_day, schedule.days)
    schedule_id = db.insert(
        "workflow_schedules",
        {
            "workflow_id": schedule.workflow_id,
            "name": schedule.name.strip(),
            "time_of_day": time_of_day,
            "days": ",".join(str(day) for day in unique_days),
            "enabled": int(schedule.enabled),
            "last_run_date": None,
        },
    )
    return {"id": schedule_id}


@app.post("/api/workflow-schedules/{schedule_id}/toggle")
def toggle_workflow_schedule(schedule_id: int) -> dict[str, Any]:
    schedule = db.find_one("workflow_schedules", {"id": schedule_id})
    if not schedule:
        raise HTTPException(status_code=404, detail="Workflow schedule not found")
    enabled = 0 if schedule["enabled"] else 1
    db.update("workflow_schedules", {"id": schedule_id}, {"enabled": enabled})
    return {"ok": True, "enabled": bool(enabled)}


@app.post("/api/schedules")
def create_schedule(schedule: ScheduleIn) -> dict[str, Any]:
    ensure_button_exists(schedule.button_id)
    time_of_day, unique_days = normalize_schedule(schedule.time_of_day, schedule.days)
    schedule_id = db.insert(
        "schedules",
        {
            "button_id": schedule.button_id,
            "name": schedule.name.strip(),
            "time_of_day": time_of_day,
            "days": ",".join(str(day) for day in unique_days),
            "enabled": int(schedule.enabled),
            "last_run_date": None,
        },
    )
    return {"id": schedule_id}


@app.post("/api/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: int) -> dict[str, Any]:
    schedule = db.find_one("schedules", {"id": schedule_id})
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    enabled = 0 if schedule["enabled"] else 1
    db.update("schedules", {"id": schedule_id}, {"enabled": enabled})
    return {"ok": True, "enabled": bool(enabled)}


def schedule_storage(schedule_kind: Literal["signal", "workflow"]) -> tuple[str, str]:
    if schedule_kind == "signal":
        return "schedules", "button_id"
    return "workflow_schedules", "workflow_id"


def ensure_schedule_target(schedule: ScheduleItemIn) -> None:
    if schedule.target_kind == "signal":
        ensure_button_exists(schedule.target_id)
    elif not db.find_one("workflows", {"id": schedule.target_id}):
        raise HTTPException(status_code=404, detail="Workflow not found")


@app.post("/api/schedule-items")
def create_schedule_item(schedule: ScheduleItemIn) -> dict[str, Any]:
    ensure_schedule_target(schedule)
    time_of_day, unique_days = normalize_schedule(schedule.time_of_day, schedule.days)
    table, target_column = schedule_storage(schedule.target_kind)
    schedule_id = db.insert(
        table,
        {
            target_column: schedule.target_id,
            "name": schedule.name.strip(),
            "time_of_day": time_of_day,
            "days": ",".join(str(day) for day in unique_days),
            "enabled": int(schedule.enabled),
            "last_run_date": None,
        },
    )
    return {"id": schedule_id, "kind": schedule.target_kind}


@app.put("/api/schedule-items/{schedule_kind}/{schedule_id}")
def update_schedule_item(
    schedule_kind: Literal["signal", "workflow"],
    schedule_id: int,
    schedule: ScheduleItemIn,
) -> dict[str, Any]:
    source_table, _ = schedule_storage(schedule_kind)
    if not db.find_one(source_table, {"id": schedule_id}):
        raise HTTPException(status_code=404, detail="Schedule not found")
    ensure_schedule_target(schedule)
    time_of_day, unique_days = normalize_schedule(schedule.time_of_day, schedule.days)
    target_table, target_column = schedule_storage(schedule.target_kind)
    values = {
        target_column: schedule.target_id,
        "name": schedule.name.strip(),
        "time_of_day": time_of_day,
        "days": ",".join(str(day) for day in unique_days),
        "enabled": int(schedule.enabled),
        "last_run_date": None,
    }
    if source_table == target_table:
        db.update(source_table, {"id": schedule_id}, values)
        updated_id = schedule_id
    else:
        db.delete(source_table, {"id": schedule_id})
        updated_id = db.insert(target_table, values)
    return {"id": updated_id, "kind": schedule.target_kind}


@app.put("/api/schedule-items/{schedule_kind}/{schedule_id}/enabled")
def set_schedule_enabled(
    schedule_kind: Literal["signal", "workflow"],
    schedule_id: int,
    value: EnabledIn,
) -> dict[str, Any]:
    table, _ = schedule_storage(schedule_kind)
    if not db.find_one(table, {"id": schedule_id}):
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.update(table, {"id": schedule_id}, {"enabled": int(value.enabled)})
    return {"ok": True, "enabled": value.enabled}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
