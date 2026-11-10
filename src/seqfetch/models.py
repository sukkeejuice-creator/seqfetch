"""Shared data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SeqFile:
    """One downloadable file.

    ``size`` is always bytes. ``md5`` is the official checksum when the provider
    can supply one, otherwise an empty string.
    """

    project: str
    run: str
    filename: str
    size: int = 0
    sample: str = ""
    md5: str = ""
    url: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.project}/{self.run}/{self.filename}"

    def relpath(self, layout: str = "{project}/{run}/{filename}") -> str:
        values = {
            "project": self.project,
            "sample": self.sample,
            "run": self.run,
            "filename": self.filename,
        }
        return layout.format(**values).replace("\\", "/").strip("/")

    @property
    def size_gib(self) -> float:
        return self.size / (1024 ** 3)


@dataclass
class SelectionRow:
    """A row of the user's sample table (or a bare accession)."""

    project: str
    sample: str = ""
    name: str = ""
    size_hint: float = 0.0
    row: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeResult:
    ok: bool
    size: Optional[int] = None
    detail: str = ""


@dataclass
class DownloadOutcome:
    status: str
    ok: bool
    bytes: int = 0
    md5: str = ""
    md5_ok: Optional[bool] = None
    seconds: float = 0.0
    attempts: int = 0
    error: str = ""
    hash_source: str = ""

    @property
    def speed_mib(self) -> float:
        if self.seconds <= 0:
            return 0.0
        return self.bytes / (1024 ** 2) / self.seconds
