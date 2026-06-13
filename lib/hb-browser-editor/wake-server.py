#!/usr/bin/env python3
"""Wake HTTP server: starts code-server on demand for Caddy forward_auth."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


USER = _env("BROWSER_EDITOR_USER", "lab")
UPSTREAM = _env("BROWSER_EDITOR_UPSTREAM", "127.0.0.1:8081")
UPSTREAM_HOST, UPSTREAM_PORT_STR = UPSTREAM.rsplit(":", 1)
UPSTREAM_PORT = int(UPSTREAM_PORT_STR)
STAMP = _env("BROWSER_EDITOR_STAMP", "/run/hb-browser-editor/last-request")
WAKE_TIMEOUT = int(_env("BROWSER_EDITOR_WAKE_TIMEOUT", "90"))
BIND = _env("BROWSER_EDITOR_WAKE_BIND", "127.0.0.1:9082")
SERVICE = f"code-server@{USER}.service"


def touch_stamp() -> None:
    stamp_dir = os.path.dirname(STAMP)
    if stamp_dir:
        os.makedirs(stamp_dir, exist_ok=True)
    with open(STAMP, "a", encoding="utf-8"):
        os.utime(STAMP, None)


def upstream_ready() -> bool:
    try:
        with socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=1):
            return True
    except OSError:
        return False


def start_code_server() -> bool:
    subprocess.run(["systemctl", "start", SERVICE], check=False)
    deadline = time.time() + WAKE_TIMEOUT
    while time.time() < deadline:
        if upstream_ready():
            return True
        time.sleep(0.5)
    return False


class WakeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/wake":
            self.send_error(404)
            return
        touch_stamp()
        if upstream_ready() or start_code_server():
            self.send_response(200)
            self.end_headers()
            return
        self.send_error(503, "code-server failed to start")

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"wake-server: {fmt % args}\n")


def main() -> None:
    host, port_str = BIND.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port_str)), WakeHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
