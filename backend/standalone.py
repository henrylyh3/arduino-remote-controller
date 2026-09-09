from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn


APP_NAME = "Smart Home Controller"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


def application_data_dir() -> Path:
    override = os.environ.get("SMART_HOME_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "smart-home-controller"


def bundled_path(*parts: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root.joinpath(*parts)


def configure_database() -> Path:
    data_dir = application_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    database = Path(os.environ.get("SMART_HOME_DB", data_dir / "smart_home.sqlite3"))
    database.parent.mkdir(parents=True, exist_ok=True)

    if not database.exists():
        seed = bundled_path("seed", "smart_home.sqlite3")
        if not seed.exists() and not getattr(sys, "frozen", False):
            seed = Path(__file__).resolve().parent / "smart_home.sqlite3"
        if seed.exists():
            shutil.copy2(seed, database)

    os.environ["SMART_HOME_DB"] = str(database)
    return database


def controller_is_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and payload.get("ok") is True
    except Exception:
        return False


def port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_socket.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def show_error(message: str) -> None:
    if sys.platform == "darwin":
        escaped = message.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'display alert "{APP_NAME}" message "{escaped}" as critical'],
            check=False,
        )
    elif os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)
    else:
        print(message, file=sys.stderr)


def wait_until_running(url: str) -> bool:
    for _ in range(120):
        if controller_is_running(url):
            return True
        time.sleep(0.25)
    return False


def stop_server(server: uvicorn.Server, server_thread: threading.Thread) -> None:
    server.should_exit = True
    server_thread.join(timeout=10)
    if server_thread.is_alive():
        server.force_exit = True
        server_thread.join(timeout=2)


def main() -> int:
    host = os.environ.get("SMART_HOME_HOST", DEFAULT_HOST)
    port = int(os.environ.get("SMART_HOME_PORT", DEFAULT_PORT))
    browser_url = f"http://127.0.0.1:{port}"

    if controller_is_running(browser_url):
        show_error(f"{APP_NAME} is already running on port {port}.")
        return 0
    if not port_is_available(port):
        show_error(f"Port {port} is already used by another application.")
        return 1

    database = configure_database()
    print(f"Database: {database}", flush=True)

    from app.main import app

    config = uvicorn.Config(app, host=host, port=port, loop="asyncio", http="h11", log_level="info")
    server = uvicorn.Server(config)

    if os.environ.get("SMART_HOME_WINDOW", "1") == "0":
        try:
            server.run()
        except KeyboardInterrupt:
            pass
        return 0

    server_thread = threading.Thread(target=server.run, daemon=True, name="smart-home-server")
    server_thread.start()
    if not wait_until_running(browser_url):
        stop_server(server, server_thread)
        show_error("The local server did not start.")
        return 1

    import webview

    webview.create_window(
        APP_NAME,
        browser_url,
        width=1280,
        height=850,
        min_size=(720, 520),
        background_color="#f4f6f3",
    )
    try:
        webview.start(gui="cocoa" if sys.platform == "darwin" else None, debug=False)
    finally:
        stop_server(server, server_thread)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
