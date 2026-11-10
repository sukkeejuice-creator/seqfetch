# seqfetch

**Download sequencing data and compute MD5 while the bytes arrive.**

`seqfetch` is a provider-agnostic batch downloader for FASTQ/BAM/CRAM and other
sequencing files. Its core trick is simple: a background thread tails the
`.part` file and feeds new bytes into a running MD5 context. When `curl` exits,
the checksum is already known, so a 20 TB batch does not need another 20 TB read
just to prove the data is intact.

GSA-Human is included as a working example, not as the design center. Any
archive that can produce a file list can drive the same pipeline through the
generic manifest provider or a ~50-line adapter.

## Why streaming MD5

Verifying after the fact means reading every byte a second time:

| Data | Disk read at 150 MB/s | Disk read at 40 MB/s |
|---|---:|---:|
| 1 TB | ~1.9 h | ~7.3 h |
| 70 TB | ~5.4 days | ~20 days |

With `verification.mode: streaming`, the hash is computed at wire speed while
the file is written. The only extra cost is one sequential read of the resume
prefix after an interrupted transfer, and even that is avoided on a clean run.

```text
curl  ──writes──▶  file.part  ──tails──▶  StreamingMD5
                         │                    │
                         └──── rename ────────┴──▶ file + md5
```

## Features

- Streaming MD5 with automatic fallback to a disk read if the hasher lags.
- Resumable `curl` transfers (`-C -`) with size checks, retries and
  re-download on checksum mismatch.
- SQLite ledger on a fixed disk: safe resume, multi-disk merges, per-file
  attempts and timings.
- Capacity-aware packing across one or many target disks.
- Optional live status columns appended to a **copy** of an Excel sample sheet.
- Instant `audit` command: compares stored MD5 values with the manifest without
  touching the disks.
- Periodic Markdown/JSON/CSV reports.
- Provider interface: GSA-Human, generic CSV/TSV manifest, or your own.

## Install

Requires Python 3.9+ and `curl`.

```bash
pip install -e ".[yaml,excel]"
# or just the core:
pip install -e .
```

If the `seqfetch` script is not on `PATH`, use `python -m seqfetch` with the
same arguments.

`PyYAML` is needed for YAML configs; `openpyxl` for Excel input and status
columns. Both are optional so the core has no runtime dependencies.

## Quick start: GSA-Human (example)

```bash
seqfetch init --provider gsa-human -o seqfetch.yaml
# edit accessions / targets / state.dir
seqfetch preflight -c seqfetch.yaml
seqfetch plan      -c seqfetch.yaml
seqfetch check     -c seqfetch.yaml --probe 1
seqfetch smoke     -c seqfetch.yaml --files 1
seqfetch run       -c seqfetch.yaml
seqfetch audit     -c seqfetch.yaml    # no disk reads
seqfetch report    -c seqfetch.yaml
```

`seqfetch init` writes a template with placeholders only. A minimal config:

```yaml
provider:
  name: gsa-human
  options:
    prefer: ftp
    md5_file: md5sum.txt

selection:
  accessions: [HRA002792]
  # or a table:
  # table: /data/samples.xlsx
  # columns: {project: HRA, sample: HRS}
  # filters: {platform: "10x Chromium"}

storage:
  targets: [/data/disk1/seq]
  reserve_gib: 200

state:
  dir: /var/lib/seqfetch/gsa-human   # fixed disk, never a removable target

verification:
  mode: streaming

reporting:
  formats: [md, json, csv]
```

## Generic manifest (ENA, SRA exports, in-house LIMS)

Any CSV/TSV with one row per file works:

```yaml
provider:
  name: manifest
  options:
    path: /data/files.tsv
    size_unit: bytes
    columns:
      project: study
      sample: sample
      run: run_accession
      filename: fastq_ftp
      size: fastq_bytes
      md5: fastq_md5
    url_template: "https://ftp.ebi.ac.uk/{filename}"
```

`filename` may also be a URL (or a semicolon-separated ENA-style URL list);
the first URL is used. `fastq_md5` lists are handled the same way.

Run the same commands: `preflight → plan → check → smoke → run → audit → report`.

## Commands

| Command | Purpose |
|---|---|
| `init` | write an example config |
| `preflight` | targets writable, free space, state outside targets, curl, provider, lock |
| `plan` | resolve accessions/table into files, attach MD5, pack onto disks |
| `check` | probe metadata, checksum manifests and the first file(s) |
| `smoke` | real download + MD5 of 1–N files into a scratch directory |
| `run` | resumable batch download with live status/report updates |
| `audit` | compare ledger MD5 strings with the manifest; **no disk reads** |
| `verify` | re-read finished files from disk (slow, optional, `--repair`) |
| `status` | print progress |
| `report` | write `report.md`, `summary.json`, `files.csv` |
| `merge` | merge a ledger produced on another disk |

Exit codes: `0` success, `1` incomplete/failed files, `2` config/preflight
error, `3` out of space, `4` interrupted. `130` on Ctrl-C.

## State and resume

All mutable state lives in `state.dir`:

```text
state/
├── plan.json          # resolved file list, URLs, MD5, target, destination
├── ledger.sqlite3     # one row per file: status, md5, disk, bytes, attempts
├── run.lock           # single-writer guard
├── cache/             # provider metadata and checksum manifests
├── logs/run.log
└── reports/           # report.md, summary.json, files.csv
```

Put this on a fixed local disk. If it lives on the removable data disk, swapping
disks fragments progress and can lose the resume state.

Resume rules:

- A finished `file` is reused only if its size matches and its MD5 matches the
  manifest.
- An incomplete `.part` is resumed with `curl -C -`; the hasher re-reads the
  existing prefix once and continues.
- A complete `.part` is hashed once before the rename.
- A checksum mismatch deletes the file and retries from zero (configurable).
- A checkpoint written across a drive dropout can still be corrupt even when
  the size looks right; the final MD5 check is what catches it.

## Status table

When `status.enabled` is true, `seqfetch` never edits the source workbook. It
writes a copy and appends prefixed columns:

`seq_status`, `seq_detail`, `seq_size_GiB`, `seq_files_done`,
`seq_files_total`, `seq_disk`, `seq_updated_at`, `seq_md5`

A sample row is marked `success` only when every resolved file succeeded and
its MD5 matched. The prefix, the row key (`sample` or `project_sample`) and the
labels are configurable under `status:` in the config.

## Writing a provider

Subclass `seqfetch.providers.base.Provider` and point `provider.name` at the
dotted path:

```python
from seqfetch.models import SeqFile
from seqfetch.providers.base import Provider

class MyArchive(Provider):
    name = "my-archive"

    def plan(self, rows, log=print):
        for row in rows:
            yield SeqFile(
                project=row.project,
                sample=row.sample,
                run="run1",
                filename="reads.fq.gz",
                size=123,
                md5="...",
                url="https://example.org/reads.fq.gz",
            )

    def md5_map(self, project, files, log=print):
        return {f.key: f.md5 for f in files}
```

```yaml
provider:
  name: "my_package.providers.MyArchive"
  options: {}
```

Only `plan()` is required. `url_for()`, `md5_map()` and `probe()` have defaults.

## Using it as an agent skill

The `skill/` directory is a ready-to-install skill for Qoder/Claude-style
agents. It encodes the intake questions, availability gate, smoke test, approval
rule for multi-day transfers, monitoring and reporting workflow.

```bash
mkdir -p .qoder/skills
cp -r skill .qoder/skills/bioinformatics-seq-data-download-skill
```

See [skill/SKILL.md](skill/SKILL.md). GSA-Human appears there as a worked
example; the workflow itself is provider-neutral.

## Design notes and limits

- **One writer, one connection.** The runner is deliberately serial. Many public
  FTP servers allow one connection per IP; parallelism belongs in a provider
  that can be sharded safely.
- **MD5, not SHA-256.** Archives publish MD5; seqfetch verifies what they
  publish. It is an integrity check, not a security primitive.
- **`plan.json` is a snapshot.** Re-run `plan` after changing the selection,
  endpoints or layout.
- **Unknown sizes** (`size: 0`) disable capacity prediction for that file; the
  runner still checks free space before each transfer.
- **Interrupted transfers** keep `.part` for resume; a later run continues.
- **The Excel status copy** is best-effort. If Excel holds the file open, the
  save is skipped and retried on the next interval.

## License

MIT. See [LICENSE](LICENSE).
