"""Generic CSV/TSV manifest provider.

Use this for any archive that can export a file list: ENA, SRA mirrors, an
in-house LIMS, a hand-written list, or an API you pre-flatten yourself.

Example::

    provider:
      name: manifest
      options:
        path: files.tsv
        columns:
          project: study
          sample: sample
          run: run_accession
          filename: fastq_ftp        # may contain a URL when url_template is absent
          size: fastq_bytes
          md5: fastq_md5
        url_template: "https://ftp.ebi.ac.uk/{filename}"
"""

from __future__ import annotations

import csv
import os
import urllib.parse
from typing import Callable, Dict, List

from ..models import SelectionRow, SeqFile
from ..util import abs_path
from .base import Provider

_UNIT = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
         "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}

COLUMNS = {
    "project": "project",
    "sample": "sample",
    "run": "run",
    "filename": "filename",
    "size": "size",
    "md5": "md5",
    "url": "url",
}


class ManifestProvider(Provider):
    name = "manifest"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        opts = self.options or {}
        self.path = abs_path(opts.get("path", ""), cfg.base) if opts.get("path") else ""
        self.columns = dict(COLUMNS)
        self.columns.update(opts.get("columns") or {})
        self.delimiter = opts.get("delimiter")
        self.size_unit = opts.get("size_unit", "bytes")
        self.url_template = opts.get("url_template", "")

    def _read(self) -> List[dict]:
        if not self.path or not os.path.exists(self.path):
            raise FileNotFoundError(f"manifest not found: {self.path}")
        delimiter = self.delimiter
        if not delimiter:
            delimiter = "\t" if self.path.lower().endswith((".tsv", ".txt")) else ","
        with open(self.path, newline="", encoding="utf-8-sig", errors="replace") as handle:
            return list(csv.DictReader(handle, delimiter=delimiter))

    def plan(self, rows: List[SelectionRow], log: Callable[[str], None] = print) -> List[SeqFile]:
        records = self._read()
        multiplier = _UNIT.get(str(self.size_unit), 1)
        files: List[SeqFile] = []
        seen = set()
        for record in records:
            project = (record.get(self.columns["project"]) or "").strip()
            run = (record.get(self.columns["run"]) or "").strip()
            filename = (record.get(self.columns["filename"]) or "").strip()
            if not filename:
                continue
            url = (record.get(self.columns["url"]) or "").strip() if self.columns.get("url") else ""
            if url and ";" in url:
                url = url.split(";")[0].strip()
            if not url and self.url_template:
                url = self.url_template.format(
                    project=project,
                    sample=(record.get(self.columns["sample"]) or "").strip(),
                    run=run,
                    filename=filename,
                    filename_q=urllib.parse.quote(filename),
                    path=urllib.parse.quote(filename, safe="/"),
                )
            if not url and "://" in filename:
                first = filename.split(";")[0].strip()
                url, filename = first, os.path.basename(urllib.parse.urlparse(first).path)
            if "://" in filename:
                filename = os.path.basename(urllib.parse.urlparse(filename.split(";")[0]).path)
            if not project:
                project = run or "default"
            if not run:
                run = os.path.basename(os.path.dirname(filename)) or "run"

            try:
                size = int(float(record.get(self.columns["size"]) or 0) * multiplier)
            except (TypeError, ValueError):
                size = 0
            md5 = (record.get(self.columns["md5"]) or "").strip().lower() if self.columns.get("md5") else ""
            if ";" in md5:
                md5 = md5.split(";")[0].strip()
            if len(md5) != 32:
                md5 = ""

            seq_file = SeqFile(
                project=project,
                sample=(record.get(self.columns["sample"]) or "").strip(),
                run=run,
                filename=filename,
                size=size,
                md5=md5,
                url=url,
            )
            if seq_file.key in seen:
                continue
            seen.add(seq_file.key)
            files.append(seq_file)
        log(f"  manifest: {len(files)} file(s) from {self.path}")
        return files

    def md5_map(
        self, project: str, files: List[SeqFile], log: Callable[[str], None] = print
    ) -> Dict[str, str]:
        return {f.key: f.md5 for f in files if f.md5}

    def url_for(self, seq_file: SeqFile) -> str:
        return seq_file.url

    def describe(self) -> Dict[str, str]:
        return {"name": self.name, "manifest": self.path, "size_unit": str(self.size_unit)}
