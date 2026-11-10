"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Dict, Optional

from . import __version__
from .check import audit, check_availability, verify_disk
from .config import ConfigError, load_config
from .ledger import Ledger
from .planner import build_plan, load_plan, plan_path
from .preflight import run_preflight
from .report import print_status, write_reports
from .runner import Runner
from .transfer import Curl, fetch
from .util import Logger, fmt_size

GSA_TEMPLATE = """version: 1

# GSA-Human is one provider. Replace this block to use another archive.
provider:
  name: gsa-human
  options:
    api_url: "https://ngdc.cncb.ac.cn/gsa-human/ajaxb/runinstudy"
    ftp_base: "ftp://download.big.ac.cn/gsa-human"
    https_base: "https://download.cncb.ac.cn/gsa-human"
    prefer: ftp
    md5_file: md5sum.txt
    cache_ttl_hours: 24

selection:
  # Either list project accessions ...
  accessions: []
  # ... or point at an XLSX/CSV table and map its columns.
  # table: /data/samples.xlsx
  # sheet: sheet1
  # columns:
  #   project: HRA
  #   sample: HRS
  #   name: samples_name
  #   size_gib: "size(GB)"
  #   platform: platform
  # filters:
  #   platform: "10x Chromium"

storage:
  targets:
    - /data/disk1/seq
    # - /data/disk2/seq
  reserve_gib: 200
  layout: "{project}/{run}/{filename}"
  order: size_desc
  on_insufficient: stop

state:
  # Must be a fixed local disk, not one of the removable targets.
  dir: /var/lib/seqfetch/gsa-human

transfer:
  curl: curl
  retries: 10
  attempts: 2
  retry_delay: 2
  speed_time: 180
  speed_limit_kbps: 1024
  ftp_method: singlecwd
  resume: true

verification:
  # streaming = hash while curl writes (recommended)
  mode: streaming
  on_mismatch: redownload
  require_official_md5: false

status:
  enabled: false
  source_table: /data/samples.xlsx
  output_table: /data/samples_status.xlsx
  prefix: seq

reporting:
  every_seconds: 1800
  formats: [md, json, csv]
"""

MANIFEST_TEMPLATE = """version: 1

provider:
  name: manifest
  options:
    path: /data/files.tsv
    delimiter: "\\t"
    size_unit: bytes
    columns:
      project: study
      sample: sample
      run: run_accession
      filename: fastq_ftp
      size: fastq_bytes
      md5: fastq_md5
      url: fastq_url
    # Used when the manifest has no URL column.
    # url_template: "https://ftp.ebi.ac.uk/{filename}"

storage:
  targets: [/data/disk1/seq]
  reserve_gib: 200
  layout: "{project}/{run}/{filename}"

state:
  dir: /var/lib/seqfetch/ena

transfer:
  retries: 10
  speed_time: 180
  speed_limit_kbps: 1024

verification:
  mode: streaming
  require_official_md5: false

reporting:
  formats: [md, json, csv]
"""


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help="config file (YAML or JSON)")
    common.add_argument("--log-file", default=argparse.SUPPRESS, help="override log path")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")

    parser = argparse.ArgumentParser(
        prog="seqfetch",
        description="Download sequencing data and compute MD5 while the bytes arrive.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"seqfetch {__version__}")
    parser.add_argument("-c", "--config", default="seqfetch.yaml", help="config file (YAML or JSON)")
    parser.add_argument("--log-file", default="", help="override log path")
    parser.add_argument("--json", action="store_true", help="JSON output where supported")
    sub = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str, parents=(common,)) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=list(parents))

    add("plan", "resolve accession/table selection into a download plan")
    p_check = add("check", "check metadata, checksum manifests and probe files")
    p_check.add_argument("--probe", type=int, default=1, help="files to probe per project (default 1)")
    p_check.add_argument("--strict", action="store_true", help="fail if any probe fails")

    p_smoke = add("smoke", "download a few files end-to-end with md5 verification")
    p_smoke.add_argument("--files", type=int, default=1)
    p_smoke.add_argument("--largest", action="store_true", help="use the largest files instead of the smallest")
    p_smoke.add_argument("--keep", action="store_true", help="keep smoke-test files")

    p_run = add("run", "download the plan (resumable, single writer)")
    p_run.add_argument("--max-files", type=int, default=0)
    p_run.add_argument("--stop-after-gib", type=float, default=0.0)
    p_run.add_argument("--skip-preflight", action="store_true")

    p_verify = add("verify", "re-read finished files from disk (slow)")
    p_verify.add_argument("--sample", default="")
    p_verify.add_argument("--limit", type=int, default=0)
    p_verify.add_argument("--repair", action="store_true", help="re-download files that fail verification")

    add("audit", "instant md5 comparison between ledger and manifest (no disk reads)")

    add("report", "write summary.json / report.md / files.csv")
    add("status", "print current progress")
    add("preflight", "check targets, state, curl, provider and lock")

    p_merge = add("merge", "merge a ledger from another disk")
    p_merge.add_argument("other", help="path to another ledger.sqlite3")

    p_init = add("init", "write an example config", parents=())
    p_init.add_argument("--provider", choices=["gsa-human", "manifest"], default="gsa-human")
    p_init.add_argument("-o", "--output", default="seqfetch.yaml")
    p_init.add_argument("--force", action="store_true")
    return parser


def _open_config(args):
    cfg = load_config(args.config)
    cfg.ensure_state()
    return cfg


def _logger(args, cfg) -> Logger:
    return Logger(args.log_file or cfg.log_file)


def cmd_plan(cfg, args, log) -> int:
    plan = build_plan(cfg, log)
    totals = plan["totals"]
    if args.json:
        print(json.dumps({"totals": totals, "targets": plan["targets"], "missing_samples": plan["missing_samples"]},
                         ensure_ascii=False, indent=2))
    else:
        print(
            f"files={totals['files']} bytes={totals['bytes']} ({fmt_size(totals['bytes'])}) "
            f"md5_known={totals['md5_known']} assigned={totals['assigned']} unassigned={totals['unassigned']}"
        )
        for target in plan["targets"]:
            print(f"target {target['path']} free={fmt_size(target.get('free_now') or 0)}")
    return 0


def cmd_check(cfg, args, log) -> int:
    plan = load_plan(cfg)
    result = check_availability(cfg, plan, log, probe_files=args.probe)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["failed_projects"]:
        log(f"[warn] {result['failed_projects']} project(s) failed the probe; downloads may still work over FTP")
    return 0 if result["ok"] or not args.strict else 1


def cmd_smoke(cfg, args, log) -> int:
    plan = load_plan(cfg)
    candidates = [r for r in plan.get("files", []) if r.get("target") and r.get("dest")]
    if args.largest:
        candidates.sort(key=lambda r: -int(r.get("size") or 0))
    else:
        candidates.sort(key=lambda r: int(r.get("size") or 0) or 1 << 62)
    chosen = candidates[: max(1, args.files)]
    if not chosen:
        log("no assigned files to smoke-test")
        return 1
    curl = Curl(cfg, log)
    failures = 0
    for record in chosen:
        root = os.path.join(record["target"], ".seqfetch_smoke")
        dest = os.path.join(root, record["relpath"])
        log(f"smoke: {record['key']} ({record.get('size_gib')} GiB) -> {dest}")
        outcome = fetch(record.get("url") or "", dest, int(record.get("size") or 0),
                        record.get("md5") or "", cfg, log, curl=curl)
        if outcome.ok:
            note = "md5 ok" if outcome.md5_ok else ("no official md5" if outcome.md5_ok is None else "md5 mismatch")
            log(f"  smoke OK ({outcome.status}, {outcome.speed_mib:.1f} MiB/s, {note})")
            if not args.keep:
                shutil.rmtree(root, ignore_errors=True)
        else:
            failures += 1
            log(f"  smoke FAILED ({outcome.status}: {outcome.error})")
    return 0 if failures == 0 else 1


def cmd_run(cfg, args, log) -> int:
    if not args.skip_preflight:
        result = run_preflight(cfg, log)
        if not result["ok"]:
            log("preflight failed; fix the checks above or pass --skip-preflight")
            return 2
    plan = load_plan(cfg)
    runner = Runner(cfg, plan, log)
    summary = runner.run(
        max_files=args.max_files or None,
        stop_after_gib=args.stop_after_gib or None,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(summary["exit_code"])


def cmd_verify(cfg, args, log) -> int:
    plan = load_plan(cfg)
    ledger = Ledger(os.path.join(cfg.state_dir, "ledger.sqlite3"))
    try:
        result = verify_disk(
            cfg, plan, ledger, log,
            limit=args.limit or None,
            repair=args.repair,
            sample=args.sample or None,
        )
        write_reports(cfg, plan, ledger, log)
    finally:
        ledger.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"checked={result['checked']} bad={result['bad']} repaired={result['repaired']}")
    return 0 if result["ok"] else 1


def cmd_audit(cfg, args, log) -> int:
    plan = load_plan(cfg)
    ledger = Ledger(os.path.join(cfg.state_dir, "ledger.sqlite3"))
    try:
        result = audit(cfg, plan, ledger)
    finally:
        ledger.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"verified={result['verified']} mismatch={result['mismatch']} "
            f"no_official_md5={result['no_official_md5']} pending={result['pending']}"
        )
        for issue in result["issues"][:20]:
            print(f"  {issue['key']}: {issue['problem']}")
    return 0 if result["ok"] else 1


def cmd_report(cfg, args, log) -> int:
    plan = load_plan(cfg)
    ledger = Ledger(os.path.join(cfg.state_dir, "ledger.sqlite3"))
    try:
        paths = write_reports(cfg, plan, ledger, log, always=True)
    finally:
        ledger.close()
    if args.json:
        print(json.dumps(paths, ensure_ascii=False, indent=2))
    return 0


def cmd_status(cfg, args, log) -> int:
    plan = load_plan(cfg, required=False)
    ledger = Ledger(os.path.join(cfg.state_dir, "ledger.sqlite3"))
    try:
        if plan:
            summary = print_status(cfg, plan, ledger) if not args.json else _summary(plan, ledger)
        else:
            summary = {"ledger_status_counts": ledger.counts(), "downloaded": {}}
    finally:
        ledger.close()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif not plan:
        print(summary)
    return 0


def _summary(plan, ledger):
    from .report import summarize

    return summarize(plan, ledger)


def cmd_preflight(cfg, args, log) -> int:
    result = run_preflight(cfg, log)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


def cmd_merge(cfg, args, log) -> int:
    ledger = Ledger(os.path.join(cfg.state_dir, "ledger.sqlite3"))
    try:
        stats = ledger.merge(args.other)
    finally:
        ledger.close()
    print(json.dumps(stats, ensure_ascii=False) if args.json else stats)
    return 0 if not stats["conflicts"] else 1


def cmd_init(args) -> int:
    output = os.path.abspath(args.output)
    if os.path.exists(output) and not args.force:
        print(f"error: {output} exists; use --force to overwrite", file=sys.stderr)
        return 2
    text = GSA_TEMPLATE if args.provider == "gsa-human" else MANIFEST_TEMPLATE
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(output)
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    if args.command == "init":
        return cmd_init(args)

    try:
        cfg = _open_config(args)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    log = _logger(args, cfg)
    handlers = {
        "plan": cmd_plan,
        "check": cmd_check,
        "smoke": cmd_smoke,
        "run": cmd_run,
        "verify": cmd_verify,
        "audit": cmd_audit,
        "report": cmd_report,
        "status": cmd_status,
        "preflight": cmd_preflight,
        "merge": cmd_merge,
    }
    try:
        return handlers[args.command](cfg, args, log)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except (ConfigError, RuntimeError, ValueError, FileNotFoundError) as exc:
        log(f"error: {exc}")
        return 2
    finally:
        log.close()
