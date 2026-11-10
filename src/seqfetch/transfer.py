"""curl-based transfers with resume, retries and streaming MD5."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .hashing import StreamingMD5, md5_file
from .models import DownloadOutcome
from .util import fmt_size

MIB = 1024 * 1024


@dataclass
class CurlResult:
    ok: bool
    returncode: int
    stderr: str
    seconds: float
    command: List[str] = field(default_factory=list)


class Curl:
    def __init__(self, cfg, log: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.log = log
        self.proc: Optional[subprocess.Popen] = None

    # -- command line ---------------------------------------------------
    def build_command(self, url: str, part: str, resume: bool) -> List[str]:
        cfg = self.cfg
        args = [
            cfg.get("transfer.curl", "curl"),
            "-sS",
            "-L",
            "--fail",
            "--connect-timeout",
            str(int(cfg.get("transfer.connect_timeout", 60))),
            "--retry",
            str(int(cfg.get("transfer.retries", 10))),
            "--retry-delay",
            str(int(cfg.get("transfer.retry_delay", 2))),
        ]
        speed_time = int(cfg.get("transfer.speed_time", 180))
        speed_limit = int(cfg.get("transfer.speed_limit_kbps", 1024))
        if speed_time > 0 and speed_limit > 0:
            args += ["--speed-time", str(speed_time), "--speed-limit", str(speed_limit * 1024)]
        if cfg.get("transfer.insecure", True):
            args.append("-k")
        if url.lower().startswith("ftp://"):
            if cfg.get("transfer.ftp_pasv", True):
                args.append("--ftp-pasv")
            method = cfg.get("transfer.ftp_method", "singlecwd")
            if method:
                args += ["--ftp-method", str(method)]
        if resume:
            args += ["-C", "-"]
        args += [str(a) for a in (cfg.get("transfer.extra_args") or [])]
        args += ["-o", part, url]
        return args

    # -- execution ------------------------------------------------------
    def run(self, url: str, part: str, resume: bool) -> CurlResult:
        command = self.build_command(url, part, resume)
        started = time.time()
        self.proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            _, stderr = self.proc.communicate()
        finally:
            code = self.proc.returncode if self.proc else -1
            self.proc = None
        return CurlResult(
            ok=(code == 0),
            returncode=int(code),
            stderr=(stderr or "").strip(),
            seconds=time.time() - started,
            command=command,
        )

    def cancel(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def probe(self, url: str, timeout: int = 60) -> dict:
        """Ask the server for the object size without downloading it."""
        args = [
            self.cfg.get("transfer.curl", "curl"),
            "-sS",
            "-I",
            "--max-time",
            str(timeout),
            "--fail",
        ]
        if self.cfg.get("transfer.insecure", True):
            args.append("-k")
        if url.lower().startswith("ftp://"):
            method = self.cfg.get("transfer.ftp_method", "singlecwd")
            if method:
                args += ["--ftp-method", str(method)]
        args.append(url)
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout + 15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "size": None, "detail": str(exc)}
        if proc.returncode != 0:
            return {"ok": False, "size": None, "detail": (proc.stderr or "").strip()}
        size = None
        match = re.search(r"Content-Length:\s*(\d+)", proc.stdout, re.I)
        if match:
            size = int(match.group(1))
        else:
            match = re.search(r"^213\s+(\d+)", proc.stdout, re.M)
            if match:
                size = int(match.group(1))
        return {"ok": True, "size": size, "detail": "ok"}


def _size_of(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def tolerance(expected: int, cfg) -> int:
    pct = float(cfg.get("transfer.size_tolerance", 0.01))
    minimum = float(cfg.get("transfer.size_tolerance_min_mib", 20)) * MIB
    return int(max(expected * pct, minimum)) if expected else 0


def fetch(
    url: str,
    dest: str,
    expected_size: int,
    expected_md5: str,
    cfg,
    log: Callable[[str], None] = print,
    curl: Optional[Curl] = None,
) -> DownloadOutcome:
    """Download one file, hashing while it is written.

    The returned outcome is successful only when the size matches and, if an
    official checksum is available, the MD5 matches.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    part = dest + ".part"
    mode = cfg.get("verification.mode", "streaming")
    tol = tolerance(expected_size, cfg)
    attempts = max(1, int(cfg.get("transfer.attempts", 2)))
    on_mismatch = cfg.get("verification.on_mismatch", "redownload")
    curl = curl or Curl(cfg, log)
    expected_md5 = (expected_md5 or "").strip().lower()

    # 1. A finished file is already in place.
    if os.path.exists(dest):
        actual = _size_of(dest)
        size_ok = (not expected_size) or abs(actual - expected_size) <= tol
        if size_ok:
            local = ""
            if expected_md5 and mode != "none":
                log("    existing file: computing md5 ...")
                local = md5_file(dest)
                if local != expected_md5:
                    log("    existing file md5 mismatch, deleting and re-downloading")
                    os.remove(dest)
                else:
                    return DownloadOutcome(
                        status="EXISTS", ok=True, bytes=actual, md5=local, md5_ok=True,
                        hash_source="read",
                    )
            else:
                return DownloadOutcome(
                    status="EXISTS", ok=True, bytes=actual, md5="", md5_ok=None,
                    hash_source="none",
                )
        else:
            log("    existing file has wrong size, deleting")
            os.remove(dest)

    # 2. A complete .part from a previous run: one read is unavoidable.
    if expected_size and os.path.exists(part):
        actual = _size_of(part)
        if actual > expected_size + tol:
            log("    stale .part is larger than expected, deleting")
            os.remove(part)
        elif abs(actual - expected_size) <= tol:
            local = ""
            md5_ok: Optional[bool] = None
            if mode != "none":
                log("    complete .part found: computing md5 before rename ...")
                local = md5_file(part)
                if expected_md5:
                    md5_ok = local == expected_md5
                    if not md5_ok:
                        os.remove(part)
                        log("    .part md5 mismatch, discarded")
                        return DownloadOutcome(
                            status="MD5_MISMATCH", ok=False, bytes=0, md5=local,
                            md5_ok=False, error="partial file md5 mismatch",
                        )
            os.replace(part, dest)
            return DownloadOutcome(
                status="OK_MD5" if md5_ok else "OK", ok=True, bytes=actual,
                md5=local, md5_ok=md5_ok, hash_source="read" if local else "none",
            )

    # 3. Transfer loop.
    last_error = ""
    last_status = "FAILED"
    total_seconds = 0.0
    for attempt in range(1, attempts + 1):
        hasher: Optional[StreamingMD5] = None
        if mode == "streaming":
            hasher = StreamingMD5(part)
            hasher.start()
        started = time.time()
        try:
            result = curl.run(url, part, resume=bool(cfg.get("transfer.resume", True) and _size_of(part) > 0))
        finally:
            if hasher is not None:
                hasher.stop()
                hasher.join()
        elapsed = time.time() - started
        total_seconds += elapsed
        actual = _size_of(part)

        if not result.ok:
            last_error = result.stderr or f"curl exit {result.returncode}"
            last_status = "FAILED"
            if cfg.get("transfer.delete_partial_on_error", False) and actual > 0:
                try:
                    os.remove(part)
                except OSError:
                    pass
            log(f"    attempt {attempt}/{attempts} failed: {last_error}")
            if attempt < attempts:
                time.sleep(float(cfg.get("transfer.retry_delay", 2)))
            continue

        if expected_size and abs(actual - expected_size) > tol:
            last_status = "SIZE_MISMATCH"
            last_error = f"size mismatch: expected {expected_size}, got {actual}"
            if actual > expected_size + tol:
                try:
                    os.remove(part)
                except OSError:
                    pass
            log(f"    {last_error}")
            if attempt < attempts:
                continue
            break

        local = ""
        source = "none"
        if mode == "streaming" and hasher is not None:
            local = hasher.digest_if_complete(actual) or ""
            source = "stream"
            if not local and cfg.get("verification.read_backup", True):
                log(f"    streaming hash fell behind ({hasher.pos}/{actual}), reading file")
                local = md5_file(part)
                source = "read"
        elif mode == "bulk":
            log("    bulk md5 pass ...")
            local = md5_file(part)
            source = "read"

        md5_ok: Optional[bool] = None
        if expected_md5 and mode != "none":
            md5_ok = bool(local) and local == expected_md5
            if not md5_ok:
                last_status = "MD5_MISMATCH"
                last_error = "md5 mismatch"
                try:
                    os.remove(part)
                except OSError:
                    pass
                log(f"    md5 mismatch (local={local or '?'}, expected={expected_md5})")
                if attempt < attempts and on_mismatch == "redownload":
                    continue
                return DownloadOutcome(
                    status="MD5_MISMATCH", ok=False, bytes=actual, md5=local,
                    md5_ok=False, seconds=total_seconds, attempts=attempt,
                    error=last_error, hash_source=source,
                )

        try:
            os.replace(part, dest)
        except OSError as exc:
            return DownloadOutcome(
                status="FAILED", ok=False, bytes=actual, md5=local, md5_ok=md5_ok,
                seconds=total_seconds, attempts=attempt, error=f"rename failed: {exc}",
                hash_source=source,
            )
        return DownloadOutcome(
            status="OK_MD5" if md5_ok else "OK",
            ok=True,
            bytes=actual,
            md5=local,
            md5_ok=md5_ok,
            seconds=total_seconds,
            attempts=attempt,
            hash_source=source,
        )

    return DownloadOutcome(
        status=last_status,
        ok=False,
        bytes=_size_of(part),
        md5="",
        md5_ok=False if expected_md5 else None,
        seconds=total_seconds,
        attempts=attempts,
        error=last_error,
        hash_source="none",
    )


def verify_file(path: str, expected_size: int, expected_md5: str, cfg) -> DownloadOutcome:
    """Re-read a finished file from disk and compare it with the manifest."""
    actual = _size_of(path)
    tol = tolerance(expected_size, cfg)
    if expected_size and abs(actual - expected_size) > tol:
        return DownloadOutcome(
            status="SIZE_MISMATCH", ok=False, bytes=actual,
            error=f"expected {fmt_size(expected_size)}, got {fmt_size(actual)}",
        )
    local = md5_file(path)
    ok = (not expected_md5) or local == expected_md5.lower()
    return DownloadOutcome(
        status="OK_MD5" if ok else "MD5_MISMATCH",
        ok=ok,
        bytes=actual,
        md5=local,
        md5_ok=ok if expected_md5 else None,
        hash_source="read",
        error="" if ok else "md5 mismatch",
    )
