"""GSA-Human adapter (example of a database-specific provider).

GSA-Human exposes:
  * ``ajaxb/runinstudy?accession=HRA...`` -> JSON run list (sampleAcc, runAcc,
    runFileName, runFileSize in GiB)
  * ``ftp://download.big.ac.cn/gsa-human/<HRA>/<HRR>/<file>``
  * ``md5sum.txt`` at the project root

All endpoints and the preferred protocol are options; nothing is baked into the
downloader.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from ..http_util import get_bytes
from ..models import ProbeResult, SelectionRow, SeqFile
from ..util import atomic_write_json, read_json
from ..transfer import Curl
from .base import Provider

DEFAULTS = {
    "api_url": "https://ngdc.cncb.ac.cn/gsa-human/ajaxb/runinstudy",
    "ftp_base": "ftp://download.big.ac.cn/gsa-human",
    "https_base": "https://download.cncb.ac.cn/gsa-human",
    "prefer": "ftp",
    "md5_file": "md5sum.txt",
    "page_size": 100000,
    "metadata_workers": 4,
    "cache_ttl_hours": 24,
    "size_unit": "GiB",
}

_UNIT = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
         "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


class GSAHumanProvider(Provider):
    name = "gsa-human"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self.opts = dict(DEFAULTS)
        self.opts.update(self.options or {})
        self.cache_dir = os.path.join(cfg.state_dir, "cache", "gsa-human")
        os.makedirs(self.cache_dir, exist_ok=True)

    # -- metadata -------------------------------------------------------
    def _cache_path(self, project: str, suffix: str) -> str:
        safe = project.replace("/", "_")
        return os.path.join(self.cache_dir, f"{safe}.{suffix}.json")

    def _fresh(self, path: str) -> bool:
        ttl = float(self.opts.get("cache_ttl_hours", 24)) * 3600
        try:
            return os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl
        except OSError:
            return False

    def fetch_project(self, project: str) -> List[dict]:
        cache = self._cache_path(project, "runs")
        if self._fresh(cache):
            cached = read_json(cache)
            if isinstance(cached, list):
                return cached
        query = urllib.parse.urlencode(
            {
                "accession": project,
                "pageNo": 1,
                "pageSize": int(self.opts.get("page_size", 100000)),
                "totalCount": int(self.opts.get("page_size", 100000)),
            }
        )
        url = f"{self.opts['api_url']}?{query}"
        data = json.loads(get_bytes(url, insecure=self.cfg.get("transfer.insecure", True)).decode("utf-8"))
        runs = data.get("runViews")
        if runs is None and isinstance(data.get("data"), dict):
            runs = data["data"].get("runViews")
        out = []
        for item in runs or []:
            run = (item.get("runAcc") or "").strip()
            filename = (item.get("runFileName") or "").strip()
            if not run or not filename:
                continue
            out.append(
                {
                    "sample": (item.get("sampleAcc") or "").strip(),
                    "run": run,
                    "file": filename,
                    "size_gb": float(item.get("runFileSize") or 0),
                }
            )
        atomic_write_json(cache, out)
        return out

    # -- Provider API ---------------------------------------------------
    def plan(self, rows: List[SelectionRow], log: Callable[[str], None] = print) -> List[SeqFile]:
        by_project: Dict[str, set] = {}
        for row in rows:
            bucket = by_project.setdefault(row.project, set())
            if row.sample:
                bucket.add(row.sample)

        multiplier = _UNIT.get(str(self.opts.get("size_unit", "GiB")), 1024 ** 3)
        projects = list(by_project)
        log(f"  {len(projects)} project(s), resolving metadata ...")
        meta: Dict[str, List[dict]] = {}
        workers = max(1, int(self.opts.get("metadata_workers", 4)))
        if len(projects) == 1:
            meta[projects[0]] = self.fetch_project(projects[0])
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self.fetch_project, p): p for p in projects}
                done = 0
                for future in as_completed(futures):
                    project = futures[future]
                    done += 1
                    try:
                        meta[project] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        log(f"  [warn] metadata failed for {project}: {exc}")
                        meta[project] = []
                    if done % 20 == 0 or done == len(projects):
                        log(f"  metadata {done}/{len(projects)}")

        files: List[SeqFile] = []
        for project in projects:
            wanted = by_project[project]
            for item in meta.get(project, []):
                if wanted and item["sample"] not in wanted:
                    continue
                seq_file = SeqFile(
                    project=project,
                    sample=item["sample"],
                    run=item["run"],
                    filename=item["file"],
                    size=int(round(item["size_gb"] * multiplier)),
                )
                seq_file.url = self.url_for(seq_file)
                files.append(seq_file)
        return files

    def md5_map(
        self, project: str, files: List[SeqFile], log: Callable[[str], None] = print
    ) -> Dict[str, str]:
        cache = self._cache_path(project, "md5")
        pairs: Optional[List[List[str]]] = None
        if self._fresh(cache):
            cached = read_json(cache)
            if isinstance(cached, list):
                pairs = cached
        if pairs is None:
            pairs = []
            text = ""
            errors = []
            for base in (self.opts["https_base"], self.opts["ftp_base"]):
                url = f"{base.rstrip('/')}/{project}/{self.opts.get('md5_file', 'md5sum.txt')}"
                try:
                    text = get_bytes(url, insecure=self.cfg.get("transfer.insecure", True)).decode(
                        "utf-8", "replace"
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{url}: {exc}")
            if not text:
                log(f"  [warn] no {self.opts.get('md5_file')} for {project}: {'; '.join(errors)}")
                return {}
            for line in text.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) != 2:
                    continue
                digest, name = parts[0].strip().lower(), parts[1].strip().lstrip("*").replace("\\", "/")
                if len(digest) == 32 and all(c in "0123456789abcdef" for c in digest):
                    pairs.append([digest, name])
            atomic_write_json(cache, pairs)

        # Index by full relative path and by basename; files are unique per run.
        by_name: Dict[str, str] = {}
        for digest, name in pairs:
            clean = name.lstrip("./")
            by_name[clean] = digest
            by_name[os.path.basename(clean)] = digest
        out: Dict[str, str] = {}
        for seq_file in files:
            candidates = [
                seq_file.key,
                f"{seq_file.run}/{seq_file.filename}",
                seq_file.filename,
            ]
            for candidate in candidates:
                if candidate in by_name:
                    out[seq_file.key] = by_name[candidate]
                    break
        return out

    def url_for(self, seq_file: SeqFile) -> str:
        base = self.opts["ftp_base"] if str(self.opts.get("prefer", "ftp")).lower() == "ftp" else self.opts["https_base"]
        path = f"{seq_file.project}/{seq_file.run}/{seq_file.filename}"
        return f"{base.rstrip('/')}/{urllib.parse.quote(path, safe='/')}"

    def probe(self, seq_file: SeqFile, log: Callable[[str], None] = print) -> ProbeResult:
        # Probe over HTTPS first: it is cheap and does not disturb the FTP session.
        path = f"{seq_file.project}/{seq_file.run}/{seq_file.filename}"
        https = f"{self.opts['https_base'].rstrip('/')}/{urllib.parse.quote(path, safe='/')}"
        result = Curl(self.cfg, log).probe(https)
        if result.get("ok"):
            return ProbeResult(True, result.get("size"), "https: ok")
        return super().probe(seq_file, log)

    def describe(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "api": str(self.opts["api_url"]),
            "transport": str(self.opts.get("prefer", "ftp")),
        }
