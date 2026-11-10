"""Append live status columns to a copy of the user's sample table.

The source table is never edited. Columns are prefixed (``seq_`` by default)
and discovered on each save, so re-runs do not create duplicates.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, Optional

from .config import Config
from .ledger import Ledger
from .util import fmt_gib

SUFFIXES = [
    ("status", "status"),
    ("detail", "detail"),
    ("size_gib", "size_GiB"),
    ("files_done", "files_done"),
    ("files_total", "files_total"),
    ("disk", "disk"),
    ("updated_at", "updated_at"),
    ("md5", "md5"),
]


class StatusWriter:
    def __init__(self, cfg: Config, plan: dict, ledger: Ledger, log: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.plan = plan
        self.ledger = ledger
        self.log = log
        self.enabled = bool(cfg.get("status.enabled"))
        self.prefix = str(cfg.get("status.prefix", "seq"))
        self.source = cfg.path_of("status.source_table")
        self.output = cfg.path_of("status.output_table")
        self.last_save = 0.0
        self.every = float(cfg.get("status.every_seconds", 300))
        self._headers = [f"{self.prefix}_{suffix}" for _, suffix in SUFFIXES]
        self._plan_stats = self._build_plan_stats()

    def _build_plan_stats(self) -> Dict[str, dict]:
        stats: Dict[str, dict] = {}
        for item in self.plan.get("files", []):
            for key in {item.get("sample") or "", f"{item.get('project')}/{item.get('sample') or ''}"}:
                if not key:
                    continue
                entry = stats.setdefault(key, {"total": 0, "bytes": 0, "project": item.get("project", "")})
                entry["total"] += 1
                entry["bytes"] += int(item.get("size") or 0)
        return stats

    def maybe_update(self, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.time()
        if not force and now - self.last_save < self.every:
            return
        try:
            self.update()
            self.last_save = now
        except Exception as exc:  # noqa: BLE001 - a locked file must not stop downloads
            self.log(f"  [warn] status table not saved yet: {exc}")

    def update(self) -> None:
        try:
            import openpyxl
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("openpyxl is required for status tables: pip install 'seqfetch[excel]'") from exc

        if not os.path.exists(self.source):
            raise FileNotFoundError(f"source table not found: {self.source}")

        workbook = openpyxl.load_workbook(self.source)
        try:
            sheet_name = self.cfg.get("status.sheet", "")
            sheet = workbook[sheet_name] if sheet_name and sheet_name in workbook.sheetnames else workbook.worksheets[0]
            base_col, columns = self._locate_columns(sheet)
            self._write_headers(sheet, base_col, PatternFill, Font, Alignment)

            stats = self.ledger.sample_stats()
            by_sample = stats["by_sample"]
            by_ps = stats["by_project_sample"]
            key_mode = self.cfg.get("status.key", "sample")
            labels = self.cfg.get("status.labels") or {}
            success_label = labels.get("success", "success")
            pending_label = labels.get("pending", "false")
            skipped_label = labels.get("skipped", "-")

            selected_rows = {row["row"] for row in self.plan.get("rows", []) if row.get("row")}
            ok_fill = PatternFill("solid", fgColor="C6EFCE")
            ok_font = Font(color="006100")
            bad_fill = PatternFill("solid", fgColor="FFC7CE")
            bad_font = Font(color="9C0006")
            na_fill = PatternFill("solid", fgColor="EDEDED")

            for row in self.plan.get("rows", []):
                row_number = row.get("row") or 0
                if not row_number:
                    continue
                project, sample = row.get("project", ""), row.get("sample", "")
                lookup = sample if key_mode == "sample" and sample else f"{project}/{sample}"
                planned = self._plan_stats.get(lookup) or self._plan_stats.get(sample) or {}
                stats = by_sample.get(sample) if key_mode == "sample" and sample else by_ps.get(f"{project}/{sample}")
                total = int(planned.get("total") or (stats or {}).get("total") or 0)
                done = int((stats or {}).get("done") or 0)
                failed = int((stats or {}).get("failed") or 0)
                size_gib = fmt_gib(planned.get("bytes") or (stats or {}).get("bytes") or 0)

                if total and done >= total:
                    status = success_label
                    detail = "all files downloaded" + (" and md5 verified" if (stats or {}).get("md5") else "")
                elif done:
                    status = pending_label
                    detail = f"{done}/{total} files done" + (f", {failed} failed" if failed else "") + "; waiting for remaining files"
                elif failed:
                    status = pending_label
                    detail = f"{failed} file(s) failed; will retry on next run"
                else:
                    status = pending_label
                    detail = "pending"
                md5 = ";".join((stats or {}).get("md5") or [])
                values = [
                    status,
                    detail,
                    float(size_gib),
                    done,
                    total,
                    ",".join((stats or {}).get("disks") or []),
                    (stats or {}).get("updated") or time.strftime("%Y-%m-%d %H:%M:%S"),
                    md5[:32000],
                ]
                for header, value in zip(self._headers, values):
                    sheet.cell(row=row_number, column=columns[header], value=value)
                cell = sheet.cell(row=row_number, column=columns[self._headers[0]])
                cell.fill = ok_fill if status == success_label else bad_fill
                cell.font = ok_font if status == success_label else bad_font

            if self.cfg.get("status.annotate_others", True):
                selected_rows = {row["row"] for row in self.plan.get("rows", []) if row.get("row")}
                for row_number in range(2, sheet.max_row + 1):
                    if row_number in selected_rows:
                        continue
                    if sheet.cell(row=row_number, column=columns[self._headers[0]]).value is None:
                        cell = sheet.cell(row=row_number, column=columns[self._headers[0]], value=skipped_label)
                        cell.fill = na_fill

            widths = [12, 52, 14, 12, 12, 26, 20, 36]
            for header, width in zip(self._headers, widths):
                sheet.column_dimensions[get_column_letter(columns[header])].width = width

            tmp = self.output + ".tmp.xlsx"
            workbook.save(tmp)
            os.replace(tmp, self.output)
            self.log(f"  status table saved: {self.output}")
        finally:
            workbook.close()

    def _locate_columns(self, sheet) -> tuple:
        header_row = [cell.value for cell in sheet[1]]
        existing = {str(value).strip(): index + 1 for index, value in enumerate(header_row) if value}
        if self._headers[0] in existing:
            columns = {}
            for header in self._headers:
                if header in existing:
                    columns[header] = existing[header]
                else:
                    columns[header] = sheet.max_column + 1
                    sheet.cell(row=1, column=columns[header], value=header)
            return existing[self._headers[0]], columns
        base = sheet.max_column + 1
        columns = {}
        for i, header in enumerate(self._headers):
            columns[header] = base + i
            sheet.cell(row=1, column=base + i, value=header)
        return base, columns

    @staticmethod
    def _write_headers(sheet, base_col: int, PatternFill, Font, Alignment) -> None:
        fill = PatternFill("solid", fgColor="1F4E78")
        font = Font(bold=True, color="FFFFFF")
        # Headers are written by _locate_columns when appending; style them here.
        for offset in range(len(SUFFIXES)):
            cell = sheet.cell(row=1, column=base_col + offset)
            if cell.value:
                cell.fill = fill
                cell.font = font
                cell.alignment = Alignment(horizontal="center", vertical="center")
