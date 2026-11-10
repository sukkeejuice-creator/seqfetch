"""Progress summary and periodic reports."""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, List, Optional

from .config import Config
from .ledger import Ledger
from .util import atomic_write_json, atomic_write_text, fmt_size


def summarize(plan: dict, ledger: Ledger) -> Dict:
    planned = plan.get("totals", {})
    files = list(ledger.iter_files())
    by_project: Dict[str, dict] = {}
    planned_by_project: Dict[str, dict] = {}
    for item in plan.get("files", []):
        entry = planned_by_project.setdefault(item["project"], {"files": 0, "bytes": 0, "sample": set()})
        entry["files"] += 1
        entry["bytes"] += int(item.get("size") or 0)
        if item.get("sample"):
            entry["sample"].add(item["sample"])

    done_files = failed_files = md5_verified = unverified = 0
    done_bytes = 0
    failures: List[dict] = []
    for row in files:
        entry = by_project.setdefault(
            row["project"], {"files": 0, "bytes": 0, "md5": 0, "failed": 0}
        )
        entry["files"] += 1
        entry["bytes"] += int(row.get("size_bytes") or 0)
        if row.get("status") == "success":
            done_files += 1
            done_bytes += int(row.get("size_bytes") or 0)
            if row.get("md5_ok") == 1:
                md5_verified += 1
                entry["md5"] += 1
            elif row.get("md5"):
                unverified += 1
        elif row.get("status") in ("FAILED", "SIZE_MISMATCH", "MD5_MISMATCH"):
            failed_files += 1
            entry["failed"] += 1
            failures.append(
                {
                    "key": row["key"],
                    "status": row.get("status"),
                    "detail": row.get("detail") or "",
                    "disk": row.get("disk") or "",
                }
            )

    projects = []
    for project in sorted(planned_by_project, key=lambda p: -planned_by_project[p]["bytes"]):
        plan_entry = planned_by_project[project]
        actual = by_project.get(project, {"files": 0, "bytes": 0, "md5": 0, "failed": 0})
        projects.append(
            {
                "project": project,
                "planned_files": plan_entry["files"],
                "planned_bytes": plan_entry["bytes"],
                "samples": len(plan_entry["sample"]),
                "done_files": actual["files"],
                "done_bytes": actual["bytes"],
                "md5_verified": actual["md5"],
                "failed": actual["failed"],
            }
        )

    counts = ledger.counts()
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "plan_created_at": plan.get("created_at", ""),
        "provider": plan.get("provider", {}),
        "planned": planned,
        "downloaded": {
            "files": done_files,
            "bytes": done_bytes,
            "md5_verified": md5_verified,
            "md5_unverified": unverified,
            "failed": failed_files,
        },
        "ledger_status_counts": counts,
        "disks": ledger.disk_usage(),
        "projects": projects,
        "failure_count": len(failures),
        "failures": failures[:200],
        "missing_samples": plan.get("missing_samples", []),
        "unassigned": plan.get("unassigned", []),
    }


def render_markdown(summary: dict) -> str:
    planned = summary["planned"]
    downloaded = summary["downloaded"]
    planned_bytes = planned.get("bytes") or 0
    done_bytes = downloaded.get("bytes") or 0
    percent = (done_bytes / planned_bytes * 100) if planned_bytes else 0.0
    lines = [
        "# seqfetch report",
        "",
        f"- Generated: {summary['generated_at']}",
        f"- Plan created: {summary.get('plan_created_at') or '-'}",
        f"- Provider: {summary.get('provider', {}).get('name', '-')}",
        f"- Files: {downloaded['files']} / {planned.get('files', 0)} ({percent:.1f}% by size)",
        f"- Volume: {fmt_size(done_bytes)} / {fmt_size(planned_bytes)}",
        f"- MD5: {downloaded['md5_verified']} verified, {downloaded['md5_unverified']} success without official md5",
        f"- Failures: {downloaded['failed']}",
        f"- Unassigned (no space): {len(summary.get('unassigned', []))}",
        "",
        "## Storage",
        "",
        "| disk | files | size |",
        "|---|---:|---:|",
    ]
    for disk, usage in sorted(summary.get("disks", {}).items()):
        lines.append(f"| {disk or '-'} | {usage['files']} | {fmt_size(usage['bytes'])} |")
    lines += [
        "",
        "## Projects",
        "",
        "| project | samples | planned files | planned | done files | done | md5 | failed |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary.get("projects", []):
        lines.append(
            f"| {item['project']} | {item['samples']} | {item['planned_files']} | "
            f"{fmt_size(item['planned_bytes'])} | {item['done_files']} | {fmt_size(item['done_bytes'])} | "
            f"{item['md5_verified']} | {item['failed']} |"
        )
    if summary.get("failures"):
        lines += ["", "## Failures", "", "| file | status | detail | disk |", "|---|---|---|---|"]
        for item in summary["failures"]:
            detail = str(item.get("detail") or "").replace("|", "/").replace("\n", " ")[:120]
            lines.append(f"| {item['key']} | {item.get('status')} | {detail} | {item.get('disk')} |")
    if summary.get("missing_samples"):
        lines += ["", "## Samples with no resolved files", ""]
        lines += [f"- {name}" for name in summary["missing_samples"]]
    return "\n".join(lines) + "\n"


def write_reports(
    cfg: Config,
    plan: dict,
    ledger: Ledger,
    log: Callable[[str], None] = print,
    always: bool = False,
) -> Dict[str, str]:
    out_dir = cfg.path_of("reporting.dir") or os.path.join(cfg.state_dir, "reports")
    os.makedirs(out_dir, exist_ok=True)
    formats = [str(f).lower() for f in (cfg.get("reporting.formats") or ["md", "json", "csv"])]
    summary = summarize(plan, ledger)
    paths: Dict[str, str] = {}
    if "json" in formats:
        paths["json"] = os.path.join(out_dir, "summary.json")
        atomic_write_json(paths["json"], summary)
    if "md" in formats:
        paths["md"] = os.path.join(out_dir, "report.md")
        atomic_write_text(paths["md"], render_markdown(summary))
    if "csv" in formats:
        paths["csv"] = os.path.join(out_dir, "files.csv")
        ledger.export_csv(paths["csv"])
    if log and (always or paths):
        log(f"  report: {paths.get('md') or paths.get('json')}")
    return paths


def print_status(cfg: Config, plan: dict, ledger: Ledger) -> Dict:
    summary = summarize(plan, ledger)
    planned = summary["planned"]
    downloaded = summary["downloaded"]
    print(
        f"files {downloaded['files']}/{planned.get('files', 0)}  "
        f"{fmt_size(downloaded['bytes'])}/{fmt_size(planned.get('bytes', 0))}  "
        f"md5 {downloaded['md5_verified']}  failed {downloaded['failed']}  "
        f"unassigned {len(summary.get('unassigned', []))}"
    )
    return summary
