"""Configuration loading, defaults and validation.

Nothing here knows about a particular database. Provider-specific endpoints live
in the provider options, so the same config schema works for GSA-Human, ENA,
SRA mirrors or an in-house archive.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, List

from .util import abs_path, expand_path

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "provider": {"name": "", "options": {}},
    "selection": {},
    "storage": {
        "targets": [],
        "reserve_gib": 0,
        "layout": "{project}/{run}/{filename}",
        "order": "size_desc",
        "on_insufficient": "stop",
        "create_dirs": False,
    },
    "state": {"dir": ".seqfetch"},
    "transfer": {
        "curl": "curl",
        "workers": 1,
        "retries": 10,
        "attempts": 2,
        "retry_delay": 2,
        "connect_timeout": 60,
        "speed_limit_kbps": 1024,
        "speed_time": 180,
        "ftp_pasv": True,
        "ftp_method": "singlecwd",
        "insecure": True,
        "extra_args": [],
        "resume": True,
        "delete_partial_on_error": False,
        "size_tolerance": 0.01,
        "size_tolerance_min_mib": 20,
        "max_consecutive_failures": 5,
    },
    "verification": {
        "mode": "streaming",
        "read_backup": True,
        "on_mismatch": "redownload",
        "require_official_md5": False,
    },
    "status": {
        "enabled": False,
        "source_table": "",
        "output_table": "",
        "sheet": "",
        "key": "sample",
        "prefix": "seq",
        "annotate_others": True,
        "labels": {"success": "success", "pending": "false", "skipped": "-"},
        "every_seconds": 300,
    },
    "reporting": {
        "every_seconds": 1800,
        "formats": ["md", "json", "csv"],
    },
}

_MISSING = object()


class ConfigError(ValueError):
    pass


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, data: Dict[str, Any], path: str) -> None:
        self.data = data
        self.path = os.path.abspath(path)
        self.base = os.path.dirname(self.path)

    # -- access ---------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str, label: str = "") -> Any:
        value = self.get(dotted, _MISSING)
        if value is _MISSING or value in ("", None, []):
            raise ConfigError(f"config field '{dotted}' is required" + (f" ({label})" if label else ""))
        return value

    def path_of(self, dotted: str) -> str:
        value = self.get(dotted, "")
        return abs_path(value, self.base) if value else ""

    @property
    def state_dir(self) -> str:
        value = self.get("state.dir", ".seqfetch")
        return abs_path(value, self.base)

    @property
    def targets(self) -> List[str]:
        return [abs_path(t, self.base) for t in (self.get("storage.targets") or [])]

    @property
    def log_file(self) -> str:
        return os.path.join(self.state_dir, "logs", "run.log")

    @property
    def provisioned(self) -> bool:
        return bool(self.data.get("_provisioned"))

    def ensure_state(self) -> None:
        for sub in ("", "logs", "reports", "cache"):
            os.makedirs(os.path.join(self.state_dir, sub), exist_ok=True)

    def validate(self) -> None:
        provider = self.get("provider.name", "")
        if not provider:
            raise ConfigError("provider.name is required")
        targets = self.targets
        if not targets:
            raise ConfigError("storage.targets must contain at least one path")
        if not (self.get("selection.accessions") or self.get("selection.table") or provider == "manifest"
                or "." in provider):
            # A custom provider may source its own file list; only built-ins are strict.
            raise ConfigError("selection.accessions or selection.table is required")

        state = os.path.normcase(os.path.abspath(self.state_dir))
        for target in targets:
            root = os.path.normcase(os.path.abspath(target))
            try:
                common = os.path.commonpath([state, root])
            except ValueError:
                continue
            if common == root:
                raise ConfigError(
                    "state.dir must live on a fixed local disk, outside storage.targets "
                    f"(state={state}, target={root})"
                )

        status = self.get("status", {})
        if status.get("enabled"):
            src = self.path_of("status.source_table")
            dst = self.path_of("status.output_table")
            if not src or not dst:
                raise ConfigError("status.source_table and status.output_table are required when status is enabled")
            if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
                raise ConfigError("status.output_table must be a copy; refusing to edit the source table in place")


def load_config(path: str, overrides: Dict[str, Any] = None) -> Config:
    from .util import read_json

    path = os.path.abspath(expand_path(path))
    if not os.path.exists(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = os.path.expandvars(text)
    if path.lower().endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ConfigError("PyYAML is required for YAML configs: pip install 'seqfetch[yaml]'") from exc
        raw = yaml.safe_load(text) or {}
    elif path.lower().endswith(".json"):
        raw = read_json(path, {}) or {}
    else:
        raise ConfigError("config must be .yaml, .yml or .json")

    data = _deep_merge(DEFAULTS, raw)
    if overrides:
        data = _deep_merge(data, overrides)
    cfg = Config(data, path)
    cfg.validate()
    return cfg
