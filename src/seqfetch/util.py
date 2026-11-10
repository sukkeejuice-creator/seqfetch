"""Small helpers shared across modules."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Optional


def expand_path(value: str) -> str:
    """Expand ``~``, ``$VAR`` and ``%VAR%`` without touching the rest."""
    return os.path.expanduser(os.path.expandvars(str(value)))


def abs_path(value: str, base: str) -> str:
    value = expand_path(value)
    if not os.path.isabs(value):
        value = os.path.join(base, value)
    return os.path.normpath(value)


def fmt_gib(n: float) -> str:
    return f"{n / (1024 ** 3):.2f}"


def fmt_size(n: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    n = float(n)
    for unit in units:
        if abs(n) < 1024.0 or unit == units[-1]:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PiB"


def fmt_seconds(seconds: float) -> str:
    if seconds is None or seconds <= 0 or seconds > 1e9:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding=encoding) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: str, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Logger:
    """Timestamped logger that mirrors to stdout and an optional file."""

    def __init__(self, file_path: Optional[str] = None) -> None:
        self.file_path = file_path
        self._fh = None
        self._lock = threading.Lock()
        if file_path:
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            self._fh = open(file_path, "a", encoding="utf-8", buffering=1)
        try:
            sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    def __call__(self, msg: str) -> None:
        line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
        with self._lock:
            print(line, flush=True)
            if self._fh is not None:
                self._fh.write(line + "\n")
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
