---
name: bioinformatics-seq-data-download-skill
description: Plan, smoke-test, batch-download and verify sequencing data from any archive (GSA-Human is the worked example) with MD5 computed while the transfer runs, a resumable SQLite ledger on a fixed disk, capacity-aware disk packing, background monitoring, status-table annotation and periodic reports. Use for HRA/HRS/HRR or ENA/SRA/custom FASTQ batches, 分批/断点下载, md5 校验, 下载状态表, 下载报告.
---

# Sequencing data download

## Overview

Use the `seqfetch` package in this repository (or an installed copy) to download
large sequencing batches and prove integrity without a second full read. The
workflow is provider-neutral: GSA-Human appears only as a concrete adapter.

Core invariant: **hash while downloading**. `verification.mode: streaming`
tails the `.part` file and finishes MD5 when `curl` exits. A post-hoc read of a
70 TB batch can take days; do not make it the default.

## Non-negotiable rules

1. **State on a fixed disk.** `state.dir` (plan, ledger, lock, cache, reports)
   must not live on a removable target. A disk swap must not fragment progress.
2. **Never edit the source table.** Status goes to a separate copy.
3. **One writer per state dir and one connection per provider.** The runner is
   serial on purpose; do not start a second instance to "speed it up".
4. **Success means all files + size + MD5.** If an official MD5 exists, a file
   is not done until it matches. Size alone is not verification.
5. **Streaming MD5 is the default.** Use `mode: bulk` only when the provider
   cannot tolerate a concurrent reader, and say so explicitly.
6. **Get explicit approval** before a multi-TB / multi-day transfer: selection,
   destinations, batch order, expected duration and stop conditions.
7. **Stop cleanly, never guess.** If state, checksum manifest, target disk or
   single-instance status is inconsistent, stop and report; do not skip files
   silently.
8. **Preserve partials.** A failed transfer keeps `.part` for resume. Delete it
   only on checksum mismatch, oversized stale partials, or explicit request.

## Phase 0 - Intake

Confirm before writing any config:

- Source: project/sample/run accessions, a file-list URL, or a table path.
- Provider: `gsa-human`, `manifest`, or a custom dotted provider.
- Scope: everything, one platform, or listed samples; table column names and
  filters.
- Storage: target root(s), mount order, reserve, one disk at a time or several
  mounted; expected volume vs free space.
- State: fixed local directory, outside every target.
- Marking: source table, output copy, prefix, key (`sample` or
  `project_sample`), success/pending labels.
- Transfer: batch vs staged, resume allowed, retry budget, speed floor.
- Approval: start now or after review; who stops the run if a disk drops.

Ask only what changes the result. If the user already supplied a table and
targets, proceed with sensible defaults and state them.

## Phase 1 - Config

```bash
seqfetch init --provider gsa-human -o seqfetch.yaml
# or: seqfetch init --provider manifest -o seqfetch.yaml
```

Then edit, do not hard-code: endpoints stay under `provider.options`; column
names under `selection.columns`; paths under `storage.targets` and `state.dir`.

A provider adapter is the only place that knows a database. If the user's
archive is not built in, write a `Provider` subclass (see
`references/providers.md`) before downloading.

## Phase 2 - Preflight

```bash
seqfetch preflight -c seqfetch.yaml
```

Require all critical checks: state writable, every target mounted and writable,
free space above reserve, `curl` present, provider importable, status source
present, no live lock. Fix failures; do not pass `--skip-preflight` on a
multi-day run.

## Phase 3 - Availability

```bash
seqfetch plan  -c seqfetch.yaml
seqfetch check -c seqfetch.yaml --probe 1 --strict
```

`plan` resolves accessions to files and attaches official MD5 values. Confirm:

- File count and total bytes match the table's expectation.
- MD5 coverage is high; if a project has no `md5sum.txt`, say so and decide
  whether size-only success is acceptable (`require_official_md5: false` is a
  conscious choice, not a default to hide).
- Probes succeed for at least one file per project.
- Selected samples that resolved to zero files are reported
  (`missing_samples`); resolve them or record the exception.

Probe failures are warnings unless `--strict`. A HEAD/HTTPS failure can coexist
with a working FTP path; do not conclude the data is gone from one endpoint.

## Phase 4 - Smoke test

```bash
seqfetch smoke -c seqfetch.yaml --files 1
```

This downloads real bytes to `<target>/.seqfetch_smoke/`, verifies MD5, and
removes the scratch copy. It proves URL, credentials, curl flags, hashing and
local writes together. It does **not** write the ledger. Only after a passing
smoke test may the full run start.

## Phase 5 - Batch run and monitoring

```bash
seqfetch run -c seqfetch.yaml
```

Run it in the background with its log in `state/logs/run.log`. Monitor for file
completions, `OK_MD5`, repeated `curl: (9)`, checksum mismatches, and free space.

Operational cadence:

- Tail the log for `OK (...)` and `FAILED (...)` events.
- Confirm the status copy updates (default every 300 s; Excel holding the file
  open defers the save, it does not corrupt it).
- Confirm planned and downloaded GiB stay consistent.
- Watch USB/disk health. On a dropout: stop, check cable/power, remount, run
  preflight, resume. The ledger plus `.part` continues; the next completed file
  still gets a full MD5 check.
- Do not start the next disk's batch while the current one is still writing.

For staged disks, fill one target, stop cleanly (`Ctrl-C` or let it return
exit 3), then mount the next and re-run `plan` or resume the same plan. Keep one
state dir for all disks and merge any per-disk ledgers with `seqfetch merge`.

## Phase 6 - Verification and report

```bash
seqfetch audit  -c seqfetch.yaml    # instant: ledger md5 vs manifest, no disk reads
seqfetch verify -c seqfetch.yaml    # optional: re-read from disk, --limit/--sample
seqfetch report -c seqfetch.yaml
```

`audit` is the normal end-of-batch check: it proves the stored MD5 equals the
official value without touching the data disks. Use `verify` only when you
suspect hardware, a drive dropout, or a ledger transplanted from elsewhere;
`--repair` re-downloads failures.

The report includes planned vs downloaded volume, MD5 verified count, per-project
progress, failures, disks used, and samples with no resolved files. Report
limitations explicitly: missing official MD5, unassigned files, interrupted
files still holding `.part`.

## Stop conditions

| Condition | Action |
|---|---|
| Target missing / read-only / below reserve | Stop; do not mark files failed |
| `state.dir` inside a target | Stop; fix config before any download |
| Live `run.lock` | Stop; never delete a lock held by a live PID |
| Repeated `curl: (9)` beyond retry budget | Stop; report endpoint and time |
| MD5 mismatch on a fresh download | One automatic re-download; then stop and report |
| MD5 mismatch on a resumed `.part` | Delete partial, re-download, re-verify |
| Consecutive failures reach threshold | Stop; inspect before hammering the server |
| User interrupt | Stop after the current transfer; keep `.part`; save status/report |

## Pitfalls

- **A size-correct resumed partial can still be corrupt.** The final MD5 is the
  only reliable check; never trust size after a drive dropout.
- **HTTPS and FTP can disagree.** A 404 on one endpoint does not mean the file
  is gone; re-probe the other and retry later.
- **Some FTP servers require `--ftp-method singlecwd`.** The default multicwd
  fails with `curl: (9) CWD denied`. Keep it in the config for such providers.
- **One connection per IP.** Serial transfers are a server constraint, not a
  missing feature.
- **`md5sum.txt` may not exist.** Record coverage; do not silently claim
  verification.
- **Unknown file sizes** (`size: 0`) make capacity planning optimistic; check
  free space per file and stop before the disk fills.
- **Excel locks.** A failed status save is retried; it must not abort downloads.
- **State on the data disk** is the most common resumability failure.

## Resources

- `references/gsa-human.md` - endpoints, fields, FTP flags, verified quirks.
- `references/providers.md` - manifest columns, URL templates, custom adapters,
  check semantics.
- `references/state-and-batching.md` - state layout, ledger/status schemas,
  capacity math, merge and report rules.
- `assets/config.example.yaml` - provider-neutral starting point.
- Repository `README.md` - CLI reference and design notes.
