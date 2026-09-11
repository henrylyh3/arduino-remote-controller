from __future__ import annotations

import json
import os
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


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def configure_mongo() -> Path:
    data_dir = application_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    explicit = os.environ.get("SMART_HOME_CONFIG")
    config_path = Path(explicit).expanduser() if explicit else data_dir / "config.env"
    if not getattr(sys, "frozen", False) and not explicit:
        load_env_file(Path(__file__).resolve().parent / ".env")
    load_env_file(config_path)
    if not os.environ.get("MONGO_URI", "").strip():
        raise RuntimeError(f"MONGO_URI is missing. Add it to {config_path}")
    os.environ.setdefault("MONGO_DB", "smart_controller")
    return config_path


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


def ask_retry(message: str) -> bool:
    if sys.platform == "darwin":
        escaped = message.replace("\\", "\\\\").replace('"', '\\"')
        result = subprocess.run(
            [
                "osascript",
                "-e",
                (
                    f'display dialog "{escaped}" with title "{APP_NAME}" '
                    'buttons {"Quit", "Retry"} default button "Retry" '
                    'cancel button "Quit" with icon stop'
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and "Retry" in result.stdout
    if os.name == "nt":
        import ctypes

        return ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x15) == 4

    print(message, file=sys.stderr)
    return input("Retry? [y/N]: ").strip().lower() in {"y", "yes"}


def wait_until_running(url: str, server_thread: threading.Thread | None = None) -> bool:
    for _ in range(120):
        if controller_is_running(url):
            return True
        if server_thread is not None and not server_thread.is_alive():
            return False
        time.sleep(0.25)
    return False


def format_startup_error(error: Exception | None) -> str:
    if error is None:
        return "The local server did not start. Check the application logs for details."

    detail = str(error).strip() or type(error).__name__
    mongo_uri = os.environ.get("MONGO_URI", "").strip()
    if mongo_uri:
        detail = detail.replace(mongo_uri, "<redacted MongoDB URI>")
    detail = detail[:700]

    if type(error).__name__ == "ServerSelectionTimeoutError":
        detail = detail.split(" (configured timeouts", 1)[0]
        return (
            "Cannot connect to MongoDB. Check the internet connection and ensure "
            "MongoDB Atlas Network Access allows this computer's current public IP.\n\n"
            f"{type(error).__name__}: {detail}"
        )
    return f"Application startup failed.\n\n{type(error).__name__}: {detail}"


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

    # Windowed executables have no console streams; keep startup diagnostics in a log.
    if sys.stdout is None or sys.stderr is None:
        data_dir = application_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        log_stream = (data_dir / "controller.log").open(
            "a", encoding="utf-8", buffering=1
        )
        if sys.stdout is None:
            sys.stdout = log_stream
        if sys.stderr is None:
            sys.stderr = log_stream

    while True:
        try:
            config_path = configure_mongo()
            break
        except Exception as exc:
            if not ask_retry(format_startup_error(exc)):
                return 1
    print(
        f"MongoDB database: {os.environ['MONGO_DB']} (config: {config_path})",
        flush=True,
    )

    from app.main import app

    if os.environ.get("SMART_HOME_WINDOW", "1") == "0":
        config = uvicorn.Config(
            app, host=host, port=port, loop="asyncio", http="h11", log_level="info"
        )
        server = uvicorn.Server(config)
        try:
            server.run()
        except KeyboardInterrupt:
            pass
        return 0

    while True:
        app.state.startup_error = None
        config = uvicorn.Config(
            app, host=host, port=port, loop="asyncio", http="h11", log_level="info"
        )
        server = uvicorn.Server(config)
        server_thread = threading.Thread(
            target=server.run, daemon=True, name="smart-home-server"
        )
        server_thread.start()
        if wait_until_running(browser_url, server_thread):
            break

        stop_server(server, server_thread)
        startup_error = getattr(app.state, "startup_error", None)
        if not ask_retry(format_startup_error(startup_error)):
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
