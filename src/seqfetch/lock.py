"""Single-instance lock so two processes never write the same ledger."""

from __future__ import annotations

import os
import subprocess
import sys


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            output = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            ).stdout
            return str(pid) in output
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class RunLock:
    def __init__(self, state_dir: str) -> None:
        self.path = os.path.join(state_dir, "run.lock")
        self.acquired = False

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(str(os.getpid()))
                self.acquired = True
                return
            except FileExistsError:
                try:
                    with open(self.path, encoding="ascii") as handle:
                        pid = int((handle.read() or "0").strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid_alive(pid):
                    raise RuntimeError(
                        f"another seqfetch run is active (pid {pid}); lock: {self.path}"
                    )
                try:
                    os.remove(self.path)
                except OSError:
                    pass
            except OSError as exc:
                raise RuntimeError(f"cannot create lock {self.path}: {exc}") from exc
        raise RuntimeError(f"cannot acquire lock {self.path}")

    def release(self) -> None:
        if self.acquired:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
