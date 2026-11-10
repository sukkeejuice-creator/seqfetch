from __future__ import annotations

import json

import pytest

from conftest import StubConfig
from seqfetch.config import ConfigError, load_config
from seqfetch.models import SelectionRow, SeqFile
from seqfetch.providers.gsa_human import GSAHumanProvider
from seqfetch.providers.manifest import ManifestProvider


def test_manifest_provider_reads_columns_and_template(tmp_path):
    manifest = tmp_path / "files.tsv"
    manifest.write_text(
        "study\tsample\trun\tfile\tbytes\tmd5\n"
        "PRJ1\tS1\tR1\treads_1.fq.gz\t1024\tabcdef0123456789abcdef0123456789\n"
        "PRJ1\tS1\tR1\treads_2.fq.gz\t2048\t\n",
        encoding="utf-8",
    )
    cfg = StubConfig(
        {
            "provider": {
                "options": {
                    "path": str(manifest),
                    "delimiter": "\t",
                    "size_unit": "bytes",
                    "columns": {
                        "project": "study",
                        "sample": "sample",
                        "run": "run",
                        "filename": "file",
                        "size": "bytes",
                        "md5": "md5",
                    },
                    "url_template": "https://example.org/{run}/{filename}",
                }
            }
        },
        base=str(tmp_path),
    )
    provider = ManifestProvider(cfg)
    files = provider.plan([])
    assert len(files) == 2
    assert files[0].size == 1024
    assert files[0].url == "https://example.org/R1/reads_1.fq.gz"
    assert files[0].md5.startswith("abcdef")
    assert provider.md5_map("PRJ1", files)["PRJ1/R1/reads_1.fq.gz"].startswith("abcdef")


def test_manifest_url_column_and_multi_value(tmp_path):
    manifest = tmp_path / "files.csv"
    manifest.write_text(
        "project,run,url,size\n"
        "P,R,https://example.org/a.fq.gz;https://mirror/a.fq.gz,10\n",
        encoding="utf-8",
    )
    cfg = StubConfig(
        {
            "provider": {
                "options": {
                    "path": str(manifest),
                    "columns": {
                        "project": "project",
                        "run": "run",
                        "url": "url",
                        "filename": "url",
                        "size": "size",
                    },
                    "size_unit": "bytes",
                }
            }
        },
        base=str(tmp_path),
    )
    files = ManifestProvider(cfg).plan([])
    assert files[0].url == "https://example.org/a.fq.gz"
    assert files[0].filename == "a.fq.gz"


def test_gsa_md5_map(monkeypatch, tmp_path):
    cfg = StubConfig(
        {"provider": {"options": {}}, "transfer": {"insecure": True}},
        state_dir=str(tmp_path / "state"),
    )
    provider = GSAHumanProvider(cfg)
    text = (
        "8d61092bb0b0ad85527fb5365edc3d01  HRA002792/HRR732176/HRR732176_f1.fq.gz\n"
        "85c8a5036439375dbd5f68826709252a  *HRA002792/HRR732176/HRR732176_r2.fq.gz\n"
    )
    monkeypatch.setattr("seqfetch.providers.gsa_human.get_bytes", lambda *a, **k: text.encode())
    files = [
        SeqFile("HRA002792", "HRR732176", "HRR732176_f1.fq.gz"),
        SeqFile("HRA002792", "HRR732176", "HRR732176_r2.fq.gz"),
    ]
    mapping = provider.md5_map("HRA002792", files)
    assert mapping["HRA002792/HRR732176/HRR732176_f1.fq.gz"] == "8d61092bb0b0ad85527fb5365edc3d01"
    assert mapping["HRA002792/HRR732176/HRR732176_r2.fq.gz"] == "85c8a5036439375dbd5f68826709252a"


def test_gsa_plan_filters_samples(monkeypatch, tmp_path):
    cfg = StubConfig(
        {
            "provider": {"options": {"size_unit": "GiB"}},
            "transfer": {"insecure": True},
        },
        state_dir=str(tmp_path / "state"),
    )
    provider = GSAHumanProvider(cfg)
    payload = {
        "runViews": [
            {"sampleAcc": "HRS1", "runAcc": "HRR1", "runFileName": "a.fq.gz", "runFileSize": 1.5},
            {"sampleAcc": "HRS2", "runAcc": "HRR2", "runFileName": "b.fq.gz", "runFileSize": 2.0},
        ]
    }
    monkeypatch.setattr(
        "seqfetch.providers.gsa_human.get_bytes", lambda *a, **k: json.dumps(payload).encode()
    )
    rows = [SelectionRow(project="HRA1", sample="HRS1")]
    files = provider.plan(rows)
    assert len(files) == 1
    assert files[0].sample == "HRS1"
    assert files[0].size == int(1.5 * 1024 ** 3)
    assert files[0].url.startswith("ftp://")


def test_config_rejects_state_inside_target(tmp_path):
    config = tmp_path / "seqfetch.yaml"
    config.write_text(
        "provider:\n  name: manifest\n"
        "storage:\n  targets:\n    - " + str(tmp_path / "data") + "\n"
        "state:\n  dir: " + str(tmp_path / "data" / "state") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(str(config))
