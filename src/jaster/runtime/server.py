from __future__ import annotations

import json
import re
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Callable


class SSEBroadcaster:
    """Thread-safe SSE client registry and broadcaster."""

    def __init__(self) -> None:
        self._clients: list[Callable[[], None] | None] = []
        self._lock = threading.Lock()
        self._latest_tree: dict | None = None

    def add_client(self) -> Callable[[], None]:
        """Register a client and return an unregister function."""

        def unregister() -> None:
            with self._lock:
                self._clients.remove(unregister)

        with self._lock:
            self._clients.append(unregister)
        return unregister

    def broadcast(self, event_type: str, data: dict) -> None:
        """Send event to all connected SSE clients."""
        self._latest_tree = data
        dead = []
        with self._lock:
            for client in self._clients:
                try:
                    client()
                except Exception:
                    dead.append(client)
            for d in dead:
                self._clients.remove(d)

    def latest_tree(self) -> dict | None:
        return self._latest_tree


class SSEHandler(SimpleHTTPRequestHandler):
    broadcaster: SSEBroadcaster | None = None
    web_root: Path | None = None
    data_root: Path | None = None

    def do_GET(self) -> None:
        if self.path == "/events":
            self._handle_events()
        elif self.path == "/current":
            self._handle_current()
        elif self.path == "/latest_run":
            self._handle_latest_run()
        elif self.path.startswith("/run/"):
            self._handle_run(self.path[5:])
        else:
            super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/tree_update":
            self._handle_tree_update()
        else:
            self.send_error(404)

    def _handle_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        broadcaster = self.broadcaster
        if not broadcaster:
            return

        unregister = broadcaster.add_client()

        try:
            latest = broadcaster.latest_tree()
            if latest:
                self.wfile.write(
                    f"event: tree_update\ndata: {json.dumps(latest, ensure_ascii=False)}\n\n".encode()
                )
            while True:
                if not broadcaster._clients:
                    break
                import time

                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            unregister()

    def _handle_current(self) -> None:
        tree = {}
        broadcaster = self.broadcaster
        if broadcaster:
            tree = broadcaster.latest_tree() or {}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(tree, ensure_ascii=False).encode())

    def _handle_run(self, path: str) -> None:
        # path like "faf7da08eb7a/tree"
        match = re.match(r"^([a-f0-9]+)(?:/tree)?$", path)
        if not match:
            self.send_error(400)
            return
        run_id = match.group(1)
        if self.data_root:
            tree_path = self.data_root / run_id / "tree.json"
        else:
            tree_path = Path(__file__).resolve().parents[2] / "data" / "runs" / run_id / "tree.json"
        if not tree_path.exists():
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(tree_path.read_text(encoding="utf-8").encode())

    def _handle_latest_run(self) -> None:
        latest_run_id = ""
        latest_mtime = -1.0
        if self.data_root and self.data_root.exists():
            for run_dir in self.data_root.iterdir():
                if not run_dir.is_dir():
                    continue
                tree_path = run_dir / "tree.json"
                if not tree_path.exists():
                    continue
                mtime = tree_path.stat().st_mtime
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_run_id = run_dir.name
        payload = {"run_id": latest_run_id}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())

    def _handle_tree_update(self) -> None:
        broadcaster = self.broadcaster
        if not broadcaster:
            self.send_error(500)
            return
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
            broadcaster.broadcast("tree_update", data)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except json.JSONDecodeError:
            self.send_error(400)

    def log_message(self, format: str, *args) -> None:
        pass  # silence default logging


def start_server(broadcaster: SSEBroadcaster, host: str = "0.0.0.0", port: int = 8765, data_root: Path | None = None) -> None:
    """Start HTTP server with SSE support in a background thread."""
    web_dir = Path(__file__).parent.parent / "web"
    SSEHandler.web_root = web_dir
    SSEHandler.broadcaster = broadcaster
    SSEHandler.data_root = data_root

    class Handler(SSEHandler):
        def translate_path(self, path: str) -> str:
            if path == "/":
                return str(web_dir / "index.html")
            return str(web_dir / path.lstrip("/"))

    httpd = HTTPServer((host, port), Handler)
    print(f"[*] SSE server running on http://{host}:{port}", flush=True)
    httpd.serve_forever()
