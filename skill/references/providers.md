# Providers

A provider turns a selection into `SeqFile` records, supplies official MD5
values, builds URLs, and optionally probes availability.

## Interface

```python
class Provider:
    def plan(self, rows: list[SelectionRow], log=print) -> list[SeqFile]: ...
    def md5_map(self, project, files, log=print) -> dict[str, str]: ...
    def url_for(self, seq_file) -> str: ...
    def probe(self, seq_file, log=print) -> ProbeResult: ...
```

Only `plan()` is abstract. `md5_map()` defaults to per-file `md5` values,
`url_for()` returns `seq_file.url`, and `probe()` issues a `curl -I`.

`SeqFile` fields: `project`, `sample`, `run`, `filename`, `size` (bytes),
`md5`, `url`, `meta`. `key` is `<project>/<run>/<filename>` and is the ledger
primary key.

## Built-in: manifest

For ENA/SRA exports, LIMS dumps, or any pre-flattened file list.

```yaml
provider:
  name: manifest
  options:
    path: /data/files.tsv
    delimiter: "\t"          # optional; inferred from extension
    size_unit: bytes         # bytes, KB, MB, GB, KiB, MiB, GiB, TiB
    columns:
      project: study
      sample: sample
      run: run_accession
      filename: fastq_ftp
      size: fastq_bytes
      md5: fastq_md5
      url: ""                # optional
    url_template: "https://ftp.ebi.ac.uk/{filename}"
```

Behavior:

- `filename` may contain a URL or a semicolon-separated URL list; the first
  URL is used and the basename becomes `filename`.
- `md5` may be semicolon-separated; the first 32-hex value is used.
- `url_template` placeholders: `{project}`, `{sample}`, `{run}`, `{filename}`,
  `{filename_q}` (URL-encoded), `{path}` (slash-preserving encoded).
- If neither `url` nor `url_template` is present, `filename` must be a URL.
- Rows with the same `key` are de-duplicated.
- `size: 0` is allowed but weakens capacity planning.

## Custom provider

```python
# my_package/archive.py
from seqfetch.models import SeqFile
from seqfetch.providers.base import Provider

class MyArchive(Provider):
    name = "my-archive"

    def plan(self, rows, log=print):
        files = []
        for row in rows:
            for remote in self._list(row.project):
                files.append(SeqFile(
                    project=row.project,
                    sample=remote.sample,
                    run=remote.run,
                    filename=remote.name,
                    size=remote.size,
                    md5=remote.md5,
                    url=f"https://archive.example/{remote.path}",
                ))
        return files
```

```yaml
provider:
  name: "my_package.archive.MyArchive"
  options:
    endpoint: "https://archive.example/api"
```

The class must subclass `Provider`. Options are available as
`self.options`; the full config as `self.cfg`; state as `self.cfg.state_dir`.

## Selection rows

When `selection.table` is used, the planner reads the table and passes
`SelectionRow(project, sample, name, size_hint, row, extra)` to the provider.
`row` is the 1-based spreadsheet row used for status annotation.

When `selection.accessions` is used, rows have an empty `sample`, meaning
"everything under this project".

## Checksum coverage

`plan` merges `md5_map()` into each file. Rules:

- Exact MD5 → success requires a match.
- No official MD5 → success requires only size; the report calls it out.
- Set `verification.require_official_md5: true` to make missing MD5 a failure
  in `audit`.

## Availability

`seqfetch check` calls `probe()` for the first N files per project and reports
MD5 coverage. Use `--strict` when a failed probe must block the run.

HEAD can fail where GET works (some mirrors, some FTP servers). Prefer the
provider's native transport for probes and treat a single failure as a warning
until a retry confirms it.
