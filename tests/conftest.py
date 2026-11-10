from __future__ import annotations

import contextlib
import http.server
import os
import threading
from typing import Dict

import pytest


class StubConfig:
    """Minimal stand-in for seqfetch.config.Config in unit tests."""

    def __init__(self, data: Dict, base: str = "", state_dir: str = ""):
        self.data = data
        self.base = base
        self._state_dir = state_dir

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def state_dir(self) -> str:
        return self._state_dir


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    root = ""
    requests = []

    def _path(self) -> str:
        return os.path.join(self.root, self.path.lstrip("/").split("?")[0])

    def do_HEAD(self):  # noqa: N802
        path = self._path()
        if not os.path.exists(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = self._path()
        if not os.path.exists(path):
            self.send_error(404)
            return
        data = open(path, "rb").read()
        range_header = self.headers.get("Range")
        self.requests.append(range_header)
        if range_header and range_header.startswith("bytes="):
            start = int(range_header.split("=", 1)[1].split("-", 1)[0] or 0)
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D401
        pass


@contextlib.contextmanager
def serve_directory(root: str):
    handler = type("Handler", (_RangeHandler,), {"root": root, "requests": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def http_dir(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    with serve_directory(str(root)) as (server, handler):
        yield server, handler, root


def base_transfer_config(**overrides):
    data = {
        "transfer": {
            "curl": "curl",
            "retries": 2,
            "attempts": 2,
            "retry_delay": 0,
            "connect_timeout": 10,
            "speed_limit_kbps": 0,
            "speed_time": 0,
            "ftp_pasv": True,
            "ftp_method": "singlecwd",
            "insecure": True,
            "extra_args": [],
            "resume": True,
            "delete_partial_on_error": False,
            "size_tolerance": 0.01,
            "size_tolerance_min_mib": 1,
        },
        "verification": {"mode": "streaming", "read_backup": True, "on_mismatch": "redownload"},
    }
    for dotted, value in overrides.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return StubConfig(data)
