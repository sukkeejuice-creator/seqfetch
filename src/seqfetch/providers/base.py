"""Provider interface.

A provider turns "what the user asked for" into a list of :class:`SeqFile`
objects, optionally supplies official MD5 values, and builds download URLs.

Adding a database means adding one subclass; nothing else in seqfetch changes.
"""

from __future__ import annotations

import importlib
from typing import Callable, Dict, List, Optional

from ..models import ProbeResult, SelectionRow, SeqFile
from ..transfer import Curl


class Provider:
    name = "base"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.options = cfg.get("provider.options") or {}

    # -- required -------------------------------------------------------
    def plan(self, rows: List[SelectionRow], log: Callable[[str], None] = print) -> List[SeqFile]:
        raise NotImplementedError

    # -- optional -------------------------------------------------------
    def md5_map(
        self, project: str, files: List[SeqFile], log: Callable[[str], None] = print
    ) -> Dict[str, str]:
        """Return checksums keyed by ``file.key`` (and optionally other aliases)."""
        return {f.key: f.md5 for f in files if f.md5}

    def url_for(self, seq_file: SeqFile) -> str:
        if not seq_file.url:
            raise ValueError(f"no URL for {seq_file.key}; set provider.options.url_template")
        return seq_file.url

    def probe(self, seq_file: SeqFile, log: Callable[[str], None] = print) -> ProbeResult:
        url = self.url_for(seq_file)
        result = Curl(self.cfg, log).probe(url)
        return ProbeResult(
            ok=bool(result.get("ok")),
            size=result.get("size"),
            detail=result.get("detail", ""),
        )

    def describe(self) -> Dict[str, str]:
        return {"name": self.name}


def load_provider(cfg) -> Provider:
    name = cfg.get("provider.name", "")
    if name == "gsa-human":
        from .gsa_human import GSAHumanProvider

        return GSAHumanProvider(cfg)
    if name == "manifest":
        from .manifest import ManifestProvider

        return ManifestProvider(cfg)
    if "." in name:
        module_name, _, class_name = name.rpartition(".")
        try:
            module = importlib.import_module(module_name)
            cls = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            raise ValueError(f"cannot load provider '{name}': {exc}") from exc
        if not issubclass(cls, Provider):
            raise TypeError(f"provider '{name}' must subclass seqfetch.providers.base.Provider")
        return cls(cfg)
    raise ValueError(
        f"unknown provider '{name}'; built-ins are 'gsa-human' and 'manifest', "
        "or use a dotted path to your own Provider subclass"
    )
