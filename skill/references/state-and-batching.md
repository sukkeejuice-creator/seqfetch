# State, ledger and batching

## State directory

Put `state.dir` on a fixed local disk, outside every `storage.targets` entry:

```text
state/
├── plan.json          # resolved file list (URL, md5, target, dest)
├── ledger.sqlite3     # one row per attempted file
├── run.lock           # pid of the single active writer
├── cache/             # provider metadata and checksum manifests
├── logs/run.log
└── reports/
    ├── report.md
    ├── summary.json
    └── files.csv
```

`run.lock` prevents two writers. If the PID is alive, stop; never delete a live
lock. A stale lock (dead PID) is replaced automatically.

## Ledger

SQLite table `files`:

| column | meaning |
|---|---|
| `key` | `<project>/<run>/<filename>` (primary key) |
| `project`, `sample`, `run`, `filename` | grouping |
| `size_bytes` | expected size |
| `md5` | local checksum computed during download |
| `md5_ok` | 1 match, 0 mismatch, NULL not applicable |
| `status` | `success`, `FAILED`, `SIZE_MISMATCH`, `MD5_MISMATCH` |
| `detail` | human-readable reason |
| `disk`, `local_path` | placement |
| `attempts`, `seconds` | transfer cost |
| `updated_at` | local timestamp |

Only `status=success` is skipped on resume. A failed file is retried on the next
run. `md5_ok=1` is required for success whenever an official MD5 exists.

## Status table

The source workbook is read-only. The output copy gets these columns (default
prefix `seq`):

| column | content |
|---|---|
| `seq_status` | `success` / pending label (`false`) / `-` |
| `seq_detail` | progress and failure explanation |
| `seq_size_GiB` | planned or downloaded size |
| `seq_files_done` | files with status success |
| `seq_files_total` | resolved files for that sample |
| `seq_disk` | target disk(s) |
| `seq_updated_at` | last ledger update |
| `seq_md5` | stored MD5 values, `;`-joined |

A row becomes `success` only when `done >= total` and every success was
checksum-verified. Rows outside the selection get the skipped label when
`status.annotate_others` is true. Re-running reuses existing columns instead of
appending duplicates.

Saves are atomic (`tmp` + `os.replace`). If Excel holds the output open, the
save is deferred and retried; downloads continue.

## Capacity and packing

Inputs: total bytes, target free space, `reserve_gib`, `order`, and
`on_insufficient`.

```text
usable = free - reserve
```

Projects are packed largest-first by default (`order: size_desc`). Files are
assigned to the current target until `usable` cannot hold the next file, then
the next target is tried. There is no splitting of a file across disks.

- `on_insufficient: stop` - leave the rest unassigned and stop cleanly.
  The status table shows them pending, never failed.
- `on_insufficient: skip` - skip files that do not fit and continue.

At execution time the runner re-checks free space before every file and may
reassign to another target; the plan is updated atomically.

## Staged disks

1. Mount disk 1, run `preflight` and `run`.
2. When it fills, the run stops with exit code 3 (out of space) or is
   interrupted. Status and report are saved.
3. Power off / unmount disk 1. Mount disk 2 at its configured path.
4. Re-run `preflight`; optionally `plan` again with the same selection.
5. `run` again. The ledger skips verified files and continues.
6. If each disk was driven by a separate state dir, merge with:

   ```bash
   seqfetch merge -c seqfetch.yaml /path/to/other/ledger.sqlite3
   ```

Merge priority: `success + md5_ok` > `success` > failed. Two different MD5
values for the same key are counted as conflicts and must be investigated.

## Reports

`report.md` contains: generated/plan timestamps, provider, files and volume
progress, MD5 verified vs unverified, failures, storage per disk, per-project
counts, and samples with no resolved files.

`summary.json` carries the same data for automation. `files.csv` is the
file-level manifest with `key,project,sample,run,filename,size_bytes,size_GiB,
md5,md5_ok,status,disk,local_path,seconds,updated_at`.

Report cadence is `reporting.every_seconds` (default 1800) plus a final write.
Status cadence is `status.every_seconds` (default 300).

## Exit codes

| code | meaning |
|---|---|
| 0 | all planned files success |
| 1 | failed or pending files remain |
| 2 | config/preflight error |
| 3 | out of space / target unavailable |
| 4 | interrupted |
| 130 | Ctrl-C in the CLI |

## Batching checklist

- [ ] One state dir for the whole campaign, on a fixed disk.
- [ ] Free space >= planned bytes + reserve + headroom for `.part`.
- [ ] Official MD5 coverage known and reported.
- [ ] Smoke test passed on the actual target.
- [ ] Single instance confirmed (no live lock).
- [ ] Stop condition agreed: disk full, failure threshold, or time box.
- [ ] Status copy path distinct from the source table.
- [ ] Report reviewed after each disk, not only at the end.
