"""Turn a selection into a concrete, disk-packed download plan."""

from __future__ import annotations

import os
import shutil
import time
from typing import Callable, Dict, List, Optional

from .config import Config
from .models import SelectionRow, SeqFile
from .providers.base import load_provider
from .util import atomic_write_json, fmt_size, read_json
from .table_source import read_table

PLAN_FILE = "plan.json"


def plan_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, PLAN_FILE)


def load_selection(cfg: Config, log: Callable[[str], None] = print) -> List[SelectionRow]:
    selection = cfg.get("selection", {}) or {}
    accessions = selection.get("accessions") or []
    if isinstance(accessions, str):
        accessions = [accessions]
    if accessions:
        return [SelectionRow(project=str(a).strip()) for a in accessions if str(a).strip()]

    table = cfg.path_of("selection.table")
    if not table:
        return []
    columns = selection.get("columns") or {}
    rows = read_table(
        table,
        sheet=selection.get("sheet", ""),
        columns=columns,
        filters=selection.get("filters") or {},
    )
    log(f"  table: {len(rows)} selected row(s) from {table}")
    return rows


def _project_totals(files: List[SeqFile]) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for item in files:
        totals[item.project] = totals.get(item.project, 0) + item.size
    return totals


def _assign_targets(
    files: List[SeqFile],
    cfg: Config,
    log: Callable[[str], None],
) -> tuple:
    targets = cfg.targets
    reserve = int(float(cfg.get("storage.reserve_gib", 0)) * (1024 ** 3))
    on_insufficient = cfg.get("storage.on_insufficient", "stop")

    items = []
    for target in targets:
        if not os.path.isdir(target):
            log(f"  [warn] target missing, skipped: {target}")
            continue
        try:
            usage = shutil.disk_usage(target)
        except OSError as exc:
            log(f"  [warn] cannot stat {target}: {exc}")
            continue
        items.append({"path": target, "free": usage.free, "total": usage.total})
    if not items:
        raise RuntimeError("no usable storage target; check storage.targets")

    assignments: Dict[str, str] = {}
    current = 0
    exhausted = False
    remaining: List[str] = []
    for index, item in enumerate(files):
        if exhausted:
            remaining.append(item.key)
            continue
        if index % 100 == 0:
            for target in items:
                try:
                    target["free"] = shutil.disk_usage(target["path"]).free
                except OSError:
                    target["free"] = 0
        need = item.size
        chosen = None
        if items[current]["free"] - reserve >= need:
            chosen = current
        else:
            for candidate in range(len(items)):
                if items[candidate]["free"] - reserve >= need:
                    chosen = candidate
                    break
        if chosen is None:
            if on_insufficient == "skip":
                continue
            exhausted = True
            remaining.append(item.key)
            continue
        current = chosen
        items[chosen]["free"] -= need
        assignments[item.key] = items[chosen]["path"]

    if remaining:
        remaining_keys = set(remaining)
        remaining_bytes = sum(f.size for f in files if f.key in remaining_keys)
        log(f"  [warn] {len(remaining)} file(s) ({fmt_size(remaining_bytes)}) do not fit in the current targets")
    return assignments, items, remaining


def build_plan(cfg: Config, log: Callable[[str], None] = print) -> dict:
    provider = load_provider(cfg)
    log(f"provider: {provider.name} {provider.describe()}")

    rows = load_selection(cfg, log)
    files = provider.plan(rows, log)
    if not files:
        raise RuntimeError("provider returned no files; check the selection")

    # De-duplicate (one run may appear twice in a component table).
    unique: Dict[str, SeqFile] = {}
    for item in files:
        unique.setdefault(item.key, item)
    files = list(unique.values())

    # Official checksums per project.
    by_project: Dict[str, List[SeqFile]] = {}
    for item in files:
        by_project.setdefault(item.project, []).append(item)
    missing_md5 = 0
    for project, project_files in by_project.items():
        mapping = provider.md5_map(project, project_files, log)
        known = 0
        for item in project_files:
            digest = mapping.get(item.key, "")
            if digest:
                item.md5 = digest
                known += 1
        missing_md5 += len(project_files) - known
        log(f"  {project}: {len(project_files)} file(s), md5 known for {known}")
    if missing_md5:
        log(f"  [warn] {missing_md5} file(s) have no official md5")

    layout = cfg.get("storage.layout", "{project}/{run}/{filename}")
    order = cfg.get("storage.order", "size_desc")
    project_bytes = _project_totals(files)
    if order == "size_desc":
        files.sort(key=lambda f: (-project_bytes[f.project], f.run, f.filename))
    else:
        files.sort(key=lambda f: (f.project, f.run, f.filename))

    relpaths = {item.key: item.relpath(layout) for item in files}
    for item in files:
        path = relpaths[item.key]
        if os.path.isabs(path) or ".." in path.split("/"):
            raise ValueError(f"unsafe layout produced path: {path}")

    assignments, items, remaining = _assign_targets(files, cfg, log)
    records = []
    for item in files:
        project = item.project
        target = assignments.get(item.key, "")
        relpath = relpaths[item.key]
        records.append(
            {
                "key": item.key,
                "project": project,
                "sample": item.sample,
                "run": item.run,
                "filename": item.filename,
                "size": item.size,
                "size_gib": round(item.size / (1024 ** 3), 3),
                "md5": item.md5,
                "url": item.url or provider.url_for(item),
                "relpath": relpath,
                "target": target,
                "dest": os.path.join(target, relpath) if target else "",
            }
        )

    selected_samples = {(row.project, row.sample) for row in rows if row.sample}
    present_samples = {(item.project, item.sample) for item in files}
    missing_samples = sorted(f"{p}/{s}" for p, s in selected_samples - present_samples)

    totals = {
        "files": len(files),
        "bytes": sum(item.size for item in files),
        "projects": len(by_project),
        "samples": len(present_samples),
        "md5_known": sum(1 for item in files if item.md5),
        "assigned": len(assignments),
        "unassigned": len(remaining),
    }
    plan = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "provider": provider.describe(),
        "targets": [
            {"path": target["path"], "total": target["total"], "free_now": _safe_free(target["path"])}
            for target in items
        ],
        "totals": totals,
        "rows": [
            {
                "row": row.row,
                "project": row.project,
                "sample": row.sample,
                "name": row.name,
                "size_hint": row.size_hint,
            }
            for row in rows
        ],
        "missing_samples": missing_samples,
        "unassigned": remaining,
        "files": records,
    }
    atomic_write_json(plan_path(cfg), plan)
    log(
        f"plan: {totals['files']} file(s), {fmt_size(totals['bytes'])}, "
        f"{totals['assigned']} assigned, {totals['unassigned']} unassigned -> {plan_path(cfg)}"
    )
    if missing_samples:
        log(f"  [warn] {len(missing_samples)} selected sample(s) resolved to no files: {missing_samples[:5]}")
    return plan


def _safe_free(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def load_plan(cfg: Config, required: bool = True) -> Optional[dict]:
    plan = read_json(plan_path(cfg))
    if plan is None and required:
        raise RuntimeError(f"no plan found at {plan_path(cfg)}; run 'seqfetch plan' first")
    return plan


def refresh_targets(plan: dict, cfg: Config) -> None:
    """Update free-space numbers in an existing plan (after mounting a disk)."""
    for target in plan.get("targets", []):
        target["free_now"] = _safe_free(target["path"])
    atomic_write_json(plan_path(cfg), plan)
