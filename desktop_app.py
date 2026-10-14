"""Windows-friendly local launcher for the built Yuragi-Strider application.

The launcher serves the Vite build and the FastAPI endpoints from one local
address, then opens the default browser. It is intentionally free of external
network calls so a packaged submission can run without API keys.
"""

from __future__ import annotations

import argparse
import logging
import mimetypes
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# PyInstaller's windowed bootloader sets these streams to None. Uvicorn and
# some dependencies expect file-like objects during startup.
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r", encoding="utf-8")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import uvicorn
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.main import app, configure_attack_simulator
from api.attack_live_websocket import MAX_WEBSOCKET_MESSAGE_BYTES
from api.attack_web_ownership import WebOwnershipSettings


def configure_launcher_logging() -> None:
    """Keep diagnostics available when the packaged app has no console."""

    log_root = Path(os.environ.get("LOCALAPPDATA", resource_root())) / "Yuragi-Strider"
    log_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_root / "launcher.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )


def resource_root() -> Path:
    """Resolve the source root or the PyInstaller extraction directory."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent


def find_available_port(host: str, preferred_port: int) -> int:
    for port in range(preferred_port, preferred_port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError("No local port is available for Yuragi-Strider.")


def configure_static_ui(ui_directory: Path) -> None:
    if not ui_directory.is_dir():
        raise RuntimeError(
            "The built frontend is missing. Run `npm.cmd run build` in frontend first."
        )

    assets_directory = ui_directory / "assets"
    if not assets_directory.is_dir():
        raise RuntimeError("The built frontend assets directory is missing.")

    # The script runner imports Pyodide's .mjs as modules and streams its .wasm;
    # browsers require the exact types, and the Windows registry may not supply them.
    mimetypes.add_type("text/javascript", ".mjs")
    mimetypes.add_type("application/wasm", ".wasm")
    app.mount("/assets", StaticFiles(directory=assets_directory), name="desktop-assets")

    ui_root = ui_directory.resolve()
    index_file = ui_root / "index.html"

    @app.get("/{requested_path:path}", include_in_schema=False)
    def desktop_spa(requested_path: str) -> FileResponse:
        if requested_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found.")

        if not requested_path:
            return FileResponse(index_file)

        # `ui_root / requested_path` is not a containment check on its own: an
        # absolute request path replaces the base outright, so "/C:/Windows/
        # win.ini" would otherwise be served verbatim. Resolve the join and
        # confirm the result is still under the built frontend; anything else
        # falls through to the SPA entry point like an unknown route.
        try:
            candidate = (ui_root / requested_path).resolve()
        except (OSError, ValueError):
            return FileResponse(index_file)

        if candidate.is_relative_to(ui_root) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_file)


def open_when_ready(url: str, host: str, port: int) -> None:
    def wait_for_server() -> None:
        for _ in range(100):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.1)
                if probe.connect_ex((host, port)) == 0:
                    webbrowser.open(url)
                    return
            time.sleep(0.1)

    threading.Thread(target=wait_for_server, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    configure_launcher_logging()
    logging.info("Starting Yuragi-Strider desktop launcher.")
    port = find_available_port(args.host, args.port)
    # The per-run cookie namespace prevents collisions between desktop instances.
    ownership_settings = WebOwnershipSettings.desktop(args.host, port)
    configure_attack_simulator(profile="desktop", ownership_settings=ownership_settings)
    ui_directory = resource_root() / "frontend" / "dist"
    # Keep the SPA fallback after API routers so it cannot shadow their paths.
    configure_static_ui(ui_directory)
    url = f"http://{args.host}:{port}/"
    if not args.no_browser:
        open_when_ready(url, args.host, port)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host,
            port=port,
            # The opt-in attack simulator uses the bundled WebSocket protocol.
            ws="websockets",
            ws_max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
            log_config=None,
            access_log=False,
        )
    )
    logging.info("Serving Yuragi-Strider at %s", url)
    server.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logging.exception("Yuragi-Strider desktop launcher failed.")
        raise
