"""Read sample rows from a user-supplied XLSX/CSV/TSV table.

Column names and filters are configuration, never hard-coded, so the same code
works for a GSA-Human export, an ENA manifest, or an internal sample sheet.
"""

from __future__ import annotations

import csv
import os
from typing import Any, Dict, Iterable, List, Optional

from .models import SelectionRow


def _norm(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _header_index(header: Iterable[Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i, name in enumerate(header):
        key = _norm(name)
        if key and key not in out:
            out[key] = i
    return out


def _matches(value: str, wanted: Any) -> bool:
    if wanted is None:
        return True
    options = wanted if isinstance(wanted, (list, tuple, set)) else [wanted]
    return any(value == _norm(option) for option in options)


def _to_rows(records: Iterable[Dict[str, Any]], columns: Dict[str, str], filters: Dict[str, Any]) -> List[SelectionRow]:
    project_col = columns.get("project", "project")
    sample_col = columns.get("sample", "sample")
    name_col = columns.get("name")
    size_col = columns.get("size_gib") or columns.get("size")
    out: List[SelectionRow] = []
    for record in records:
        if not all(_matches(_norm(record.get(col, "")), wanted) for col, wanted in (filters or {}).items()):
            continue
        project = _norm(record.get(project_col, ""))
        if not project:
            continue
        sample = _norm(record.get(sample_col, "")) if sample_col else ""
        try:
            size = float(record.get(size_col) or 0) if size_col else 0.0
        except (TypeError, ValueError):
            size = 0.0
        out.append(
            SelectionRow(
                project=project,
                sample=sample,
                name=_norm(record.get(name_col, "")) if name_col else "",
                size_hint=size,
                row=int(record.get("__row__", 0) or 0),
                extra={k: v for k, v in record.items() if not k.startswith("__")},
            )
        )
    return out


def read_table(
    path: str,
    sheet: str = "",
    columns: Optional[Dict[str, str]] = None,
    filters: Optional[Dict[str, Any]] = None,
) -> List[SelectionRow]:
    columns = dict(columns or {"project": "project", "sample": "sample"})
    filters = filters or {}
    ext = os.path.splitext(path)[1].lower()

    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(path, sheet, columns, filters)
    if ext in (".csv", ".tsv", ".txt"):
        return _read_delimited(path, columns, filters)
    raise ValueError(f"unsupported table format: {ext}")


def _read_xlsx(path: str, sheet: str, columns: Dict[str, str], filters: Dict[str, Any]) -> List[SelectionRow]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("openpyxl is required to read XLSX tables: pip install 'seqfetch[excel]'") from exc

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            return []
        index = _header_index(header)
        records = []
        for row_number, row in enumerate(rows, start=2):
            if row is None or all(v is None for v in row):
                continue
            record = {name: (row[i] if i < len(row) else None) for name, i in index.items()}
            record["__row__"] = row_number
            records.append(record)
        return _to_rows(records, {k: v for k, v in columns.items()}, filters)
    finally:
        wb.close()


def _read_delimited(path: str, columns: Dict[str, str], filters: Dict[str, Any]) -> List[SelectionRow]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(8192)
        f.seek(0)
        delimiter = "\t" if path.lower().endswith((".tsv", ".txt")) else ","
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
            delimiter = dialect.delimiter
        except csv.Error:
            pass
        reader = csv.DictReader(f, delimiter=delimiter)
        records = []
        for row_number, row in enumerate(reader, start=2):
            row["__row__"] = row_number
            records.append(row)
        return _to_rows(records, columns, filters)
