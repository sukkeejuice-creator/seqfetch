"""Environment checks before a long transfer."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Callable, Dict, List

from .config import Config
from .lock import pid_alive
from .providers.base import load_provider
from .util import fmt_size


def _probe_write(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    handle, probe = tempfile.mkstemp(prefix=".seqfetch_probe_", dir=path)
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(b"\0" * (1024 * 1024))
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def run_preflight(cfg: Config, log: Callable[[str], None] = print) -> Dict:
    checks: List[dict] = []

    def add(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "critical": critical})

    # State directory
    state = cfg.state_dir
    try:
        _probe_write(state)
        add("state writable", True, state)
    except OSError as exc:
        add("state writable", False, f"{state}: {exc}")

    # Targets
    reserve = int(float(cfg.get("storage.reserve_gib", 0)) * (1024 ** 3))
    for target in cfg.targets:
        if not os.path.isdir(target):
            add(f"target {target}", False, "does not exist or is not mounted")
            continue
        try:
            usage = shutil.disk_usage(target)
            _probe_write(target)
            enough = usage.free >= reserve
            add(
                f"target {target}",
                enough,
                f"{fmt_size(usage.free)} free, reserve {fmt_size(reserve)}"
                + ("" if enough else " (below reserve)"),
            )
        except OSError as exc:
            add(f"target {target}", False, f"not writable: {exc}")

    # curl
    curl = cfg.get("transfer.curl", "curl")
    try:
        result = subprocess.run([curl, "--version"], capture_output=True, text=True, timeout=15)
        line = (result.stdout or "").splitlines()[0] if result.stdout else ""
        add("curl", result.returncode == 0, line or curl)
    except (OSError, subprocess.SubprocessError) as exc:
        add("curl", False, f"{curl}: {exc}")

    # Provider
    try:
        provider = load_provider(cfg)
        add("provider", True, str(provider.describe()))
        if provider.name == "manifest" and not os.path.exists(getattr(provider, "path", "")):
            add("manifest file", False, str(getattr(provider, "path", "")))
    except Exception as exc:  # noqa: BLE001
        add("provider", False, str(exc))

    # Status table
    if cfg.get("status.enabled"):
        source = cfg.path_of("status.source_table")
        output = cfg.path_of("status.output_table")
        add("status source", os.path.exists(source), source)
        try:
            _probe_write(os.path.dirname(output))
            add("status output dir", True, os.path.dirname(output))
        except OSError as exc:
            add("status output dir", False, str(exc))

    # Running instance
    lock = os.path.join(state, "run.lock")
    if os.path.exists(lock):
        try:
            pid = int((open(lock, encoding="ascii").read() or "0").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        add("single instance", not pid_alive(pid), f"lock exists (pid {pid})" if pid else "stale lock")
    else:
        add("single instance", True, "no lock")

    critical_ok = all(item["ok"] for item in checks if item["critical"])
    if log:
        for item in checks:
            marker = "ok" if item["ok"] else ("FAIL" if item["critical"] else "warn")
            log(f"  [{marker}] {item['check']}: {item['detail']}")
    return {"ok": critical_ok, "checks": checks}
