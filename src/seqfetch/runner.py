"""Sequential batch runner: one connection, one writer, resumable."""

from __future__ import annotations

import os
import shutil
import signal
import time
from typing import Callable, Dict, Optional

from .config import Config
from .ledger import Ledger
from .lock import RunLock
from .models import SeqFile
from .planner import plan_path
from .report import write_reports
from .status_table import StatusWriter
from .transfer import Curl, fetch
from .util import atomic_write_json, fmt_size

GIB = 1024 ** 3


class Runner:
    def __init__(self, cfg: Config, plan: dict, log: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.plan = plan
        self.log = log
        self.ledger = Ledger(os.path.join(cfg.state_dir, "ledger.sqlite3"))
        self.curl = Curl(cfg, log)
        self.status = StatusWriter(cfg, plan, self.ledger, log)
        self.lock = RunLock(cfg.state_dir)
        self.stop_requested = False
        self.stop_reason = ""
        self.session_bytes = 0
        self.session_files = 0
        self.md5_verified = 0
        self.failures = 0
        self.skipped = 0
        self.consecutive_failures = 0
        self.last_report = time.time()

    # -- lifecycle -------------------------------------------------------
    def _install_signals(self) -> None:
        try:
            signal.signal(signal.SIGINT, self._on_signal)
        except (ValueError, OSError):
            pass

    def _on_signal(self, signum, frame) -> None:  # noqa: ARG002
        if not self.stop_requested:
            self.log("interrupt received; stopping after the current transfer")
        self.stop_requested = True
        self.stop_reason = "interrupted"
        self.curl.cancel()

    def request_stop(self, reason: str) -> None:
        self.stop_requested = True
        self.stop_reason = reason

    def run(self, max_files: Optional[int] = None, stop_after_gib: Optional[float] = None) -> Dict:
        self.lock.acquire()
        self._install_signals()
        limit_bytes = int(stop_after_gib * GIB) if stop_after_gib else None
        files = self.plan.get("files", [])
        total = len(files)
        started = time.time()
        try:
            for index, record in enumerate(files, start=1):
                if self.stop_requested:
                    break
                if max_files and self.session_files >= max_files:
                    self.stop_reason = "max_files reached"
                    break
                if limit_bytes and self.session_bytes >= limit_bytes:
                    self.stop_reason = "stop_after_gib reached"
                    break

                row = self.ledger.get(record["key"])
                if row and row.get("status") == "success":
                    self.skipped += 1
                    continue

                if not record.get("target") or not record.get("dest"):
                    self.stop_reason = "no storage assigned (insufficient space at plan time)"
                    break

                target = record["target"]
                if not os.path.isdir(target) or not self._has_space(target, int(record.get("size") or 0)):
                    target = self._reassign(record)
                    if not target:
                        self.stop_reason = f"insufficient space or target unavailable: {record.get('target')}"
                        break
                dest = record["dest"]

                self.log(
                    f"[{index}/{total}] {record['key']} "
                    f"({record.get('size_gib') or 0:.2f} GiB) -> {target}"
                )
                outcome = fetch(
                    record.get("url") or "",
                    dest,
                    int(record.get("size") or 0),
                    record.get("md5") or "",
                    self.cfg,
                    self.log,
                    curl=self.curl,
                )

                if self.stop_requested and not outcome.ok:
                    self.log("  transfer interrupted; partial file kept for resume")
                    break

                # Only a fully verified file is marked success.
                ledger_status = "success" if outcome.ok else outcome.status
                detail = outcome.status
                if outcome.ok:
                    detail = "stream-hashed" if outcome.hash_source == "stream" else (
                        "read-hashed" if outcome.hash_source == "read" else "size-only"
                    )
                    if outcome.md5_ok:
                        detail += ", md5 ok"
                else:
                    detail = outcome.error or outcome.status
                self.ledger.upsert(
                    self._seq_file(record),
                    ledger_status,
                    outcome.md5,
                    outcome.md5_ok,
                    detail,
                    target,
                    dest,
                    outcome.attempts,
                    outcome.seconds,
                )

                if outcome.ok:
                    self.session_files += 1
                    self.consecutive_failures = 0
                    if outcome.status != "EXISTS":
                        self.session_bytes += outcome.bytes
                    if outcome.md5_ok:
                        self.md5_verified += 1
                    rate = f"{outcome.speed_mib:.1f} MiB/s" if outcome.seconds > 0 else "-"
                    md5_note = "md5 ok" if outcome.md5_ok else ("no official md5" if outcome.md5_ok is None else "md5 mismatch")
                    self.log(
                        f"    OK ({outcome.status}, {rate}, {md5_note}, "
                        f"session {self.session_bytes / GIB:.1f} GiB)"
                    )
                else:
                    self.failures += 1
                    self.consecutive_failures += 1
                    self.log(f"    FAILED ({outcome.status}: {outcome.error[:200]})")
                    if self.consecutive_failures >= int(self.cfg.get("transfer.max_consecutive_failures", 5)):
                        self.stop_reason = f"{self.consecutive_failures} consecutive failures"
                        break

                self.status.maybe_update()
                if time.time() - self.last_report >= float(self.cfg.get("reporting.every_seconds", 1800)):
                    write_reports(self.cfg, self.plan, self.ledger, self.log)
                    self.last_report = time.time()
        except KeyboardInterrupt:
            self.stop_reason = "interrupted"
        finally:
            try:
                self.status.maybe_update(force=True)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[warn] final status table save failed: {exc}")
            try:
                write_reports(self.cfg, self.plan, self.ledger, self.log)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[warn] final report failed: {exc}")
            try:
                atomic_write_json(plan_path(self.cfg), self.plan)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[warn] could not update plan.json: {exc}")
            summary = self.summary(started)
            self.ledger.close()
            self.lock.release()
            self.log(
                f"batch finished: {summary['downloaded_files']} downloaded, {self.skipped} skipped, "
                f"{summary['failed_files']} failed, {fmt_size(summary['downloaded_bytes'])} moved"
                + (f"; stopped: {self.stop_reason}" if self.stop_reason else "")
            )
        return summary

    # -- helpers ---------------------------------------------------------
    def _seq_file(self, record: dict) -> SeqFile:
        return SeqFile(
            project=record.get("project", ""),
            sample=record.get("sample", ""),
            run=record.get("run", ""),
            filename=record.get("filename", ""),
            size=int(record.get("size") or 0),
            md5=record.get("md5") or "",
            url=record.get("url") or "",
        )

    def _has_space(self, target: str, need: int) -> bool:
        try:
            free = shutil.disk_usage(target).free
        except OSError:
            return False
        reserve = int(float(self.cfg.get("storage.reserve_gib", 0)) * GIB)
        return free - reserve >= need

    def _reassign(self, record: dict) -> Optional[str]:
        need = int(record.get("size") or 0)
        relpath = record.get("relpath") or ""
        for target in self.cfg.targets:
            if self._has_space(target, need):
                record["target"] = target
                record["dest"] = os.path.join(target, relpath)
                for item in self.plan.get("targets", []):
                    if item.get("path") == target:
                        try:
                            item["free_now"] = shutil.disk_usage(target).free
                        except OSError:
                            item["free_now"] = 0
                atomic_write_json(plan_path(self.cfg), self.plan)
                self.log(f"    reassigned to {target}")
                return target
        return None

    def summary(self, started: float) -> Dict:
        counts = self.ledger.counts()
        pending = 0
        failed_total = 0
        failure_states = {"FAILED", "SIZE_MISMATCH", "MD5_MISMATCH"}
        for record in self.plan.get("files", []):
            row = self.ledger.get(record["key"])
            if not row or row.get("status") != "success":
                pending += 1
                if row and row.get("status") in failure_states:
                    failed_total += 1
        completed = counts.get("success", 0)
        if self.stop_reason == "interrupted":
            exit_code = 4
        elif self.stop_reason and ("space" in self.stop_reason or "target" in self.stop_reason):
            exit_code = 3
        elif self.stop_reason in ("max_files reached", "stop_after_gib reached"):
            exit_code = 0
        elif failed_total or pending:
            exit_code = 1
        else:
            exit_code = 0
        return {
            "downloaded_files": self.session_files,
            "downloaded_bytes": self.session_bytes,
            "md5_verified": self.md5_verified,
            "skipped": self.skipped,
            "failed_files": failed_total,
            "pending_files": pending,
            "completed_total": completed,
            "seconds": time.time() - started,
            "stop_reason": self.stop_reason,
            "exit_code": exit_code,
        }
