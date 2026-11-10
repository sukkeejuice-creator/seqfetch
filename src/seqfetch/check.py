"""Availability checks, instant ledger audit and on-disk re-verification."""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, List, Optional

from .config import Config
from .ledger import Ledger
from .models import SeqFile
from .providers.base import load_provider
from .transfer import fetch, verify_file
from .util import fmt_size


def _seq_file(record: dict) -> SeqFile:
    return SeqFile(
        project=record.get("project", ""),
        sample=record.get("sample", ""),
        run=record.get("run", ""),
        filename=record.get("filename", ""),
        size=int(record.get("size") or 0),
        md5=record.get("md5") or "",
        url=record.get("url") or "",
    )


def check_availability(
    cfg: Config,
    plan: dict,
    log: Callable[[str], None] = print,
    probe_files: int = 1,
) -> Dict:
    provider = load_provider(cfg)
    grouped: Dict[str, List[dict]] = {}
    for record in plan.get("files", []):
        grouped.setdefault(record["project"], []).append(record)

    results = []
    failed_projects = 0
    for project, records in grouped.items():
        probes = []
        for record in records[: max(0, probe_files)]:
            result = provider.probe(_seq_file(record), log)
            probes.append(
                {
                    "file": record["filename"],
                    "ok": result.ok,
                    "size": result.size,
                    "detail": result.detail,
                }
            )
        md5_known = sum(1 for record in records if record.get("md5"))
        probe_ok = all(p["ok"] for p in probes) if probes else None
        if probe_ok is False:
            failed_projects += 1
        results.append(
            {
                "project": project,
                "files": len(records),
                "bytes": sum(int(r.get("size") or 0) for r in records),
                "md5_known": md5_known,
                "md5_ratio": (md5_known / len(records)) if records else 0.0,
                "probes": probes,
                "probe_ok": probe_ok,
            }
        )
        detail = "ok" if probe_ok else ("probe failed" if probe_ok is False else "not probed")
        log(
            f"  {project}: {len(records)} file(s), "
            f"{fmt_size(sum(int(r.get('size') or 0) for r in records))}, "
            f"md5 {md5_known}/{len(records)}, {detail}"
        )
        for probe in probes:
            if not probe["ok"]:
                log(f"    [warn] probe failed for {probe['file']}: {probe['detail'][:160]}")

    return {
        "ok": failed_projects == 0,
        "projects": results,
        "failed_projects": failed_projects,
    }


def audit(cfg: Config, plan: dict, ledger: Ledger) -> Dict:
    """Compare stored MD5 strings with the manifest. No disk reads."""
    ok = mismatch = missing = pending = unverified = 0
    issues: List[dict] = []
    for record in plan.get("files", []):
        row = ledger.get(record["key"])
        expected = (record.get("md5") or "").lower()
        if row is None or row.get("status") != "success":
            pending += 1
            continue
        local = (row.get("md5") or "").lower()
        if expected:
            if local != expected:
                mismatch += 1
                issues.append(
                    {"key": record["key"], "problem": "md5 differs from manifest",
                     "local": local, "expected": expected}
                )
            elif row.get("md5_ok") != 1:
                unverified += 1
                issues.append({"key": record["key"], "problem": "md5 matches but not marked verified"})
            else:
                ok += 1
        else:
            missing += 1
            if local:
                unverified += 1
            else:
                issues.append({"key": record["key"], "problem": "no md5 stored"})
    return {
        "ok": mismatch == 0 and (missing == 0 or not cfg.get("verification.require_official_md5", False)),
        "verified": ok,
        "mismatch": mismatch,
        "no_official_md5": missing,
        "unverified": unverified,
        "pending": pending,
        "issues": issues[:500],
    }


def verify_disk(
    cfg: Config,
    plan: dict,
    ledger: Ledger,
    log: Callable[[str], None] = print,
    limit: Optional[int] = None,
    repair: bool = False,
    sample: Optional[str] = None,
) -> Dict:
    """Re-read finished files from disk. Slow by design; streaming makes it optional."""
    candidates = []
    for record in plan.get("files", []):
        if sample and record.get("sample") != sample:
            continue
        row = ledger.get(record["key"])
        if row is None or row.get("status") != "success":
            continue
        path = row.get("local_path") or record.get("dest") or ""
        if path:
            candidates.append((record, row, path))
    if limit:
        candidates = candidates[:limit]

    total_bytes = sum(int(record.get("size") or 0) for record, _, _ in candidates)
    log(f"verifying {len(candidates)} file(s), {fmt_size(total_bytes)} to read")
    started = time.time()
    checked = bad = repaired = 0
    issues = []
    for index, (record, row, path) in enumerate(candidates, start=1):
        if not os.path.exists(path):
            bad += 1
            issues.append({"key": record["key"], "problem": "file missing", "path": path})
            ledger.set_status(record["key"], "FAILED", "file missing", md5_ok=False)
            continue
        outcome = verify_file(path, int(record.get("size") or 0), record.get("md5") or "", cfg)
        checked += 1
        if outcome.ok:
            ledger.set_status(
                record["key"], "success",
                f"verified on disk ({outcome.md5[:12]}...)",
                md5_ok=True if record.get("md5") else None,
            )
            if outcome.md5 and outcome.md5 != (row.get("md5") or ""):
                # Keep the disk-derived value; it is what was actually written.
                ledger.upsert(
                    _seq_file(record), "success", outcome.md5,
                    True if record.get("md5") else None, "verified on disk",
                    row.get("disk") or "", path, row.get("attempts") or 0, row.get("seconds") or 0,
                )
        else:
            bad += 1
            issues.append(
                {"key": record["key"], "problem": outcome.status, "detail": outcome.error, "path": path}
            )
            ledger.set_status(
                record["key"], outcome.status,
                outcome.error or "verification failed",
                md5_ok=False if record.get("md5") else None,
            )
            if repair:
                log(f"  repairing {record['key']} ...")
                new = fetch(record.get("url") or "", path, int(record.get("size") or 0),
                            record.get("md5") or "", cfg, log)
                if new.ok:
                    repaired += 1
                    ledger.upsert(
                        _seq_file(record), "success", new.md5,
                        True if record.get("md5") else None, "repaired after verification failure",
                        row.get("disk") or "", path, new.attempts, new.seconds,
                    )
        if index % 100 == 0:
            elapsed = time.time() - started
            log(f"  verified {index}/{len(candidates)} ({fmt_size(total_bytes * index / max(1, len(candidates)))} done, {elapsed:.0f}s)")
    return {
        "ok": bad == 0,
        "checked": checked,
        "bad": bad,
        "repaired": repaired,
        "seconds": time.time() - started,
        "issues": issues[:500],
    }
