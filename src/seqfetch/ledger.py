"""SQLite ledger: the single source of truth for resume and reporting.

The ledger lives in ``state.dir`` on a fixed disk, never on a removable data
disk, so swapping targets cannot fragment progress. Multi-disk runs can merge
ledgers with :meth:`Ledger.merge`.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Dict, Iterable, List, Optional

from .models import SeqFile
from .util import atomic_write_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    key         TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    sample      TEXT,
    run         TEXT,
    filename    TEXT,
    size_bytes  INTEGER DEFAULT 0,
    md5         TEXT,
    md5_ok      INTEGER,
    status      TEXT,
    detail      TEXT,
    disk        TEXT,
    local_path  TEXT,
    attempts    INTEGER DEFAULT 0,
    seconds     REAL DEFAULT 0,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_status  ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_sample  ON files(sample);
CREATE INDEX IF NOT EXISTS idx_files_project ON files(project);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Ledger:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- writes ----------------------------------------------------------
    def upsert(
        self,
        seq_file: SeqFile,
        status: str,
        md5: str = "",
        md5_ok: Optional[bool] = None,
        detail: str = "",
        disk: str = "",
        local_path: str = "",
        attempts: int = 0,
        seconds: float = 0.0,
    ) -> None:
        value = None if md5_ok is None else (1 if md5_ok else 0)
        self.conn.execute(
            """
            INSERT INTO files (key, project, sample, run, filename, size_bytes, md5, md5_ok,
                               status, detail, disk, local_path, attempts, seconds, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                size_bytes=excluded.size_bytes, md5=excluded.md5, md5_ok=excluded.md5_ok,
                status=excluded.status, detail=excluded.detail, disk=excluded.disk,
                local_path=excluded.local_path, attempts=excluded.attempts,
                seconds=excluded.seconds, updated_at=excluded.updated_at
            """,
            (
                seq_file.key, seq_file.project, seq_file.sample, seq_file.run, seq_file.filename,
                seq_file.size, (md5 or "").lower(), value, status, detail, disk, local_path,
                attempts, seconds, _now(),
            ),
        )
        self.conn.commit()

    def set_status(self, key: str, status: str, detail: str = "", md5_ok: Optional[bool] = None) -> None:
        value = None if md5_ok is None else (1 if md5_ok else 0)
        self.conn.execute(
            "UPDATE files SET status=?, detail=?, md5_ok=COALESCE(?, md5_ok), updated_at=? WHERE key=?",
            (status, detail, value, _now(), key),
        )
        self.conn.commit()

    # -- reads -----------------------------------------------------------
    def get(self, key: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM files WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def is_done(self, key: str) -> bool:
        row = self.conn.execute("SELECT status, md5_ok FROM files WHERE key=?", (key,)).fetchone()
        return bool(row and row["status"] == "success")

    def iter_files(self, project: Optional[str] = None, status: Optional[str] = None) -> Iterable[dict]:
        query = "SELECT * FROM files"
        clauses, params = [], []
        if project:
            clauses.append("project=?")
            params.append(project)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY project, sample, run, filename"
        for row in self.conn.execute(query, params):
            yield dict(row)

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM files GROUP BY status"):
            out[row["status"] or "unknown"] = row["n"]
        return out

    def sample_stats(self) -> Dict[str, dict]:
        """Aggregate per sample and per project/sample."""
        by_sample: Dict[str, dict] = {}
        by_project_sample: Dict[str, dict] = {}
        for row in self.conn.execute("SELECT * FROM files"):
            project, sample = row["project"] or "", row["sample"] or ""
            keys = [("ps", f"{project}/{sample}")]
            if sample:
                keys.append(("s", sample))
            for kind, key in keys:
                bucket = by_project_sample if kind == "ps" else by_sample
                stats = bucket.setdefault(
                    key,
                    {
                        "project": project,
                        "sample": sample,
                        "total": 0,
                        "done": 0,
                        "failed": 0,
                        "bad_md5": 0,
                        "bytes": 0,
                        "md5": [],
                        "disks": [],
                        "updated": "",
                        "attempted": 0,
                    },
                )
                stats["total"] += 1
                stats["attempted"] += 1
                stats["bytes"] += int(row["size_bytes"] or 0)
                if row["status"] == "success":
                    stats["done"] += 1
                    if row["md5"]:
                        stats["md5"].append(row["md5"])
                    if row["md5_ok"] == 0:
                        stats["bad_md5"] += 1
                elif row["status"] in ("FAILED", "SIZE_MISMATCH", "MD5_MISMATCH"):
                    stats["failed"] += 1
                if row["disk"] and row["disk"] not in stats["disks"]:
                    stats["disks"].append(row["disk"])
                if (row["updated_at"] or "") > stats["updated"]:
                    stats["updated"] = row["updated_at"] or ""
        return {"by_sample": by_sample, "by_project_sample": by_project_sample}

    def disk_usage(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for row in self.conn.execute(
            "SELECT disk, COUNT(*) n, COALESCE(SUM(size_bytes),0) bytes FROM files "
            "WHERE status='success' GROUP BY disk"
        ):
            out[row["disk"] or ""] = {"files": row["n"], "bytes": row["bytes"]}
        return out

    # -- merge -----------------------------------------------------------
    @staticmethod
    def _rank(row: dict) -> int:
        if row.get("status") != "success":
            return 1 if row.get("status") else 0
        if row.get("md5_ok") == 1:
            return 4
        return 3

    def merge(self, other_path: str) -> Dict[str, int]:
        """Merge another ledger (e.g. from another disk) into this one."""
        if os.path.abspath(other_path) == os.path.abspath(self.path):
            raise ValueError("cannot merge a ledger into itself")
        stats = {"inserted": 0, "updated": 0, "kept": 0, "conflicts": 0}
        other = sqlite3.connect(other_path)
        other.row_factory = sqlite3.Row
        try:
            for row in other.execute("SELECT * FROM files"):
                row = dict(row)
                current = self.get(row["key"])
                if current is None:
                    self._insert_row(row)
                    stats["inserted"] += 1
                    continue
                if (
                    current.get("md5") and row.get("md5")
                    and current.get("status") == "success" and row.get("status") == "success"
                    and current["md5"] != row["md5"]
                ):
                    stats["conflicts"] += 1
                if self._rank(row) > self._rank(current) or (
                    self._rank(row) == self._rank(current)
                    and (row.get("updated_at") or "") > (current.get("updated_at") or "")
                ):
                    self._insert_row(row)
                    stats["updated"] += 1
                else:
                    stats["kept"] += 1
        finally:
            other.close()
            self.conn.commit()
        return stats

    def _insert_row(self, row: dict) -> None:
        columns = [
            "key", "project", "sample", "run", "filename", "size_bytes", "md5", "md5_ok",
            "status", "detail", "disk", "local_path", "attempts", "seconds", "updated_at",
        ]
        self.conn.execute(
            f"INSERT OR REPLACE INTO files ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [row.get(c) for c in columns],
        )

    # -- export ----------------------------------------------------------
    def export_csv(self, path: str) -> None:
        rows = list(self.iter_files())
        header = [
            "key", "project", "sample", "run", "filename", "size_bytes", "size_GiB",
            "md5", "md5_ok", "status", "disk", "local_path", "seconds", "updated_at",
        ]
        lines = [",".join(header)]
        for row in rows:
            values = [
                row.get("key", ""), row.get("project", ""), row.get("sample", ""),
                row.get("run", ""), row.get("filename", ""), row.get("size_bytes", 0),
                f"{(row.get('size_bytes') or 0) / (1024 ** 3):.3f}", row.get("md5") or "",
                "" if row.get("md5_ok") is None else row.get("md5_ok"),
                row.get("status") or "", row.get("disk") or "", row.get("local_path") or "",
                f"{row.get('seconds') or 0:.1f}", row.get("updated_at") or "",
            ]
            lines.append(",".join(_csv_escape(str(v)) for v in values))
        atomic_write_text(path, "\n".join(lines) + "\n")


def _csv_escape(value: str) -> str:
    if any(c in value for c in ',"\n'):
        return '"' + value.replace('"', '""') + '"'
    return value
