# GSA-Human provider reference

GSA-Human is the worked example, not a dependency of the workflow.

## Accessions

- `HRA...` project/study
- `HRS...` sample
- `HRX...` experiment
- `HRR...` run (one file per run, usually `_f1`/`_r2` pairs)

One `HRS` can map to many `HRR` runs. A sample is complete only when every run's
file is complete and checksum-verified.

## Metadata API

```text
https://ngdc.cncb.ac.cn/gsa-human/ajaxb/runinstudy
  ?accession=HRA002792
  &pageNo=1
  &pageSize=100000
  &totalCount=100000
```

Response contains `runViews[]` with:

| field | meaning |
|---|---|
| `sampleAcc` | HRS accession |
| `runAcc` | HRR accession |
| `runFileName` | file name, e.g. `HRR732176_f1.fq.gz` |
| `runFileSize` | numeric size in **GiB** (1024^3), configurable via `size_unit` |

Cache the parsed list under `state/cache/gsa-human/<project>.runs.json` with a
TTL (`cache_ttl_hours`, default 24).

## Transport

```text
ftp://download.big.ac.cn/gsa-human/<HRA>/<HRR>/<file>
https://download.cncb.ac.cn/gsa-human/<HRA>/<HRR>/<file>
```

Prefer FTP for large transfers. HTTPS is useful for probes but can return 404
while the FTP object exists; never treat one endpoint's 404 as deletion.

Verified curl flags:

```text
-sS -k -L --fail
--ftp-pasv
--ftp-method singlecwd          # mandatory on this server
--retry 10 --retry-delay 2
--speed-time 180 --speed-limit 1024
-C -                            # only when .part exists
-o file.part <url>
```

`singlecwd` avoids `curl: (9) Server denied you to change to the given
directory` caused by the default multicwd strategy.

## Checksums

`<project>/md5sum.txt` is a flat list:

```text
8d61092bb0b0ad85527fb5365edc3d01  HRA002792/HRR732176/HRR732176_f1.fq.gz
85c8a5036439375dbd5f68826709252a *HRA002792/HRR732176/HRR732176_r2.fq.gz
```

The parser accepts an optional `*`, leading `./`, and backslashes, and indexes
by full relative path, `run/filename`, and basename. Cache the pairs under
`state/cache/gsa-human/<project>.md5.json`.

`md5sum.txt` may be absent or temporarily 404. Record coverage and decide with
the user whether size-only success is acceptable.

## Example

```yaml
provider:
  name: gsa-human
  options:
    api_url: "https://ngdc.cncb.ac.cn/gsa-human/ajaxb/runinstudy"
    ftp_base: "ftp://download.big.ac.cn/gsa-human"
    https_base: "https://download.cncb.ac.cn/gsa-human"
    prefer: ftp
    md5_file: md5sum.txt

selection:
  accessions: [HRA002792]
  # or
  # table: /data/samples.xlsx
  # columns: {project: HRA, sample: HRS}
  # filters: {platform: "10x Chromium"}

storage:
  targets: [/data/disk1/seq]
  reserve_gib: 200

state:
  dir: /var/lib/seqfetch/gsa-human

verification:
  mode: streaming
```

## Observed failure modes (2026-11-10)

- **Corrupt resumed prefix after a USB dropout.** A `.part` resumed across the
  dropout reached the exact expected size but failed MD5. Deleting the partial
  and downloading fresh passed. This is why success requires the checksum, not
  the size.
- **Transient 550/CWD burst.** Three to seven consecutive `curl: (9)` responses
  over ~40 s, then recovery on retry. Both HTTPS and FTP `md5sum.txt` returned
  404 during the burst while the FTP listing still showed the file. Retry with
  backoff; do not declare data loss from a single probe.
- **Fresh large-file throughput** on the same link: ~19–28 MB/s per connection,
  with streaming MD5 adding no pause at completion.

## Implementation

`src/seqfetch/providers/gsa_human.py`. Endpoints, protocol preference, cache
TTL, page size and size unit are all provider options.
