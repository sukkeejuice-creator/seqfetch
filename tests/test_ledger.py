from __future__ import annotations

import os

from seqfetch.ledger import Ledger
from seqfetch.models import SeqFile


def _file(sample="HRS1", run="HRR1", name="a.fq.gz", size=1024):
    return SeqFile(project="HRA1", sample=sample, run=run, filename=name, size=size)


def test_upsert_and_sample_stats(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    try:
        first = _file()
        second = _file(name="b.fq.gz")
        ledger.upsert(first, "success", "a" * 32, True, "md5 ok", "/mnt/d1", "/mnt/d1/a")
        ledger.upsert(second, "FAILED", "", False, "network", "/mnt/d1", "/mnt/d1/b")

        assert ledger.get(first.key)["status"] == "success"
        stats = ledger.sample_stats()["by_sample"]["HRS1"]
        assert stats["total"] == 2
        assert stats["done"] == 1
        assert stats["failed"] == 1
        assert stats["bytes"] == 2048
        assert stats["md5"] == ["a" * 32]
    finally:
        ledger.close()


def test_merge_prefers_success_and_reports_conflict(tmp_path):
    left = Ledger(str(tmp_path / "left.sqlite3"))
    right = Ledger(str(tmp_path / "right.sqlite3"))
    try:
        item = _file()
        left.upsert(item, "FAILED", "", False, "timeout")
        right.upsert(item, "success", "b" * 32, True, "md5 ok")

        stats = left.merge(str(tmp_path / "right.sqlite3"))
        assert stats["updated"] == 1
        assert left.get(item.key)["status"] == "success"
        assert left.get(item.key)["md5"] == "b" * 32
    finally:
        left.close()
        right.close()

    left = Ledger(str(tmp_path / "left.sqlite3"))
    right = Ledger(str(tmp_path / "right.sqlite3"))
    try:
        conflict = _file(name="c.fq.gz")
        left.upsert(conflict, "success", "c" * 32, True, "")
        right.upsert(conflict, "success", "d" * 32, True, "")
        stats = left.merge(str(tmp_path / "right.sqlite3"))
        assert stats["conflicts"] == 1
    finally:
        left.close()
        right.close()


def test_export_csv(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    try:
        ledger.upsert(_file(), "success", "e" * 32, True, "ok", "/mnt/d1", "/mnt/d1/a")
        out = tmp_path / "files.csv"
        ledger.export_csv(str(out))
        text = out.read_text(encoding="utf-8")
        assert text.startswith("key,project,sample")
        assert "HRA1/HRR1/a.fq.gz" in text
        assert "e" * 32 in text
    finally:
        ledger.close()
