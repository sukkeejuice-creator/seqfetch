from __future__ import annotations

import hashlib
import os

import openpyxl

from conftest import serve_directory
from seqfetch.cli import main
from seqfetch.ledger import Ledger


def _write_workbook(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "sheet1"
    ws.append(["HRA", "HRS", "sample_name", "platform"])
    for row in rows:
        ws.append(row)
    wb.save(str(path))
    wb.close()


def test_end_to_end_manifest(tmp_path):
    www = tmp_path / "www"
    www.mkdir()
    files = {
        "a.fq.gz": b"a" * (1024 * 1024),
        "b.fq.gz": b"b" * (2 * 1024 * 1024),
    }
    for name, data in files.items():
        (www / name).write_bytes(data)

    with serve_directory(str(www)) as (server, handler):
        port = server.server_address[1]
        manifest = tmp_path / "files.tsv"
        manifest.write_text(
            "study\tsample\trun\tfile\tbytes\tmd5\turl\n"
            f"HRA9\tHRS9\tR1\ta.fq.gz\t{len(files['a.fq.gz'])}\t"
            f"{hashlib.md5(files['a.fq.gz']).hexdigest()}\thttp://127.0.0.1:{port}/a.fq.gz\n"
            f"HRA9\tHRS9\tR1\tb.fq.gz\t{len(files['b.fq.gz'])}\t"
            f"{hashlib.md5(files['b.fq.gz']).hexdigest()}\thttp://127.0.0.1:{port}/b.fq.gz\n",
            encoding="utf-8",
        )

        source = tmp_path / "samples.xlsx"
        output = tmp_path / "samples_status.xlsx"
        _write_workbook(source, [["HRA9", "HRS9", "one", "10x"], ["HRA9", "HRS8", "two", "10x"]])

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        state_dir = tmp_path / "state"
        config = tmp_path / "seqfetch.yaml"
        config.write_text(
            "version: 1\n"
            "provider:\n  name: manifest\n  options:\n"
            f"    path: \"{manifest.as_posix()}\"\n"
            "    delimiter: \"\\t\"\n"
            "    size_unit: bytes\n"
            "    columns: {project: study, sample: sample, run: run, filename: file, size: bytes, md5: md5, url: url}\n"
            "selection:\n"
            f"  table: \"{source.as_posix()}\"\n"
            "  columns: {project: HRA, sample: HRS, name: sample_name, platform: platform}\n"
            "  filters: {platform: 10x}\n"
            "storage:\n"
            f"  targets: [\"{data_dir.as_posix()}\"]\n"
            "  reserve_gib: 0\n"
            "  layout: \"{project}/{run}/{filename}\"\n"
            f"state:\n  dir: \"{state_dir.as_posix()}\"\n"
            "transfer: {attempts: 1, retries: 1, speed_time: 0, speed_limit_kbps: 0}\n"
            "verification: {mode: streaming}\n"
            "status:\n"
            "  enabled: true\n"
            f"  source_table: \"{source.as_posix()}\"\n"
            f"  output_table: \"{output.as_posix()}\"\n"
            "  key: sample\n"
            "  prefix: seq\n"
            "reporting: {every_seconds: 1, formats: [md, json, csv]}\n",
            encoding="utf-8",
        )

        assert main(["plan", "-c", str(config)]) == 0
        assert main(["smoke", "-c", str(config), "--files", "1"]) == 0
        assert main(["run", "-c", str(config)]) == 0
        assert main(["audit", "-c", str(config)]) == 0
        assert main(["status", "-c", str(config)]) == 0
        assert main(["report", "-c", str(config)]) == 0

    ledger = Ledger(str(state_dir / "ledger.sqlite3"))
    try:
        rows = list(ledger.iter_files())
        assert len(rows) == 2
        assert all(row["status"] == "success" for row in rows)
        assert all(row["md5_ok"] == 1 for row in rows)
    finally:
        ledger.close()

    assert (data_dir / "HRA9" / "R1" / "a.fq.gz").exists()
    assert (data_dir / "HRA9" / "R1" / "b.fq.gz").exists()
    assert (state_dir / "reports" / "report.md").exists()
    assert (state_dir / "reports" / "summary.json").exists()

    wb = openpyxl.load_workbook(str(output))
    try:
        ws = wb.worksheets[0]
        headers = [cell.value for cell in ws[1]]
        status_col = headers.index("seq_status") + 1
        assert ws.cell(row=2, column=status_col).value == "success"
        assert ws.cell(row=3, column=status_col).value == "false"
        md5_col = headers.index("seq_md5") + 1
        stored = str(ws.cell(row=2, column=md5_col).value)
        assert all(len(part) == 32 for part in stored.split(";"))
    finally:
        wb.close()
