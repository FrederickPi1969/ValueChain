# Cosmos Deployment

## Storage Layout

The Cosmos deployment separates latency-sensitive state from the bulk corpus:

| Path | Device | Purpose |
| --- | --- | --- |
| `/home/pi/ValueChain` | NVMe/ext4 | Git checkout, source code, configuration, virtual environment |
| `/home/pi/valuechain-state/postgres` | NVMe/ext4 | PostgreSQL tables, indexes, WAL, resolver queues, graph metadata |
| `/mnt/hdd8tb/valuechain/data/raw` | 8 TB HDD | Immutable source documents and archive responses |
| `/mnt/hdd8tb/valuechain/data/processed` | 8 TB HDD | Parsed sections, passages, extraction artifacts, batch snapshots |
| `/mnt/hdd8tb/valuechain/reports` | 8 TB HDD | Generated briefs, dashboards, validation exports |
| `/mnt/hdd8tb/valuechain/imports` | 8 TB HDD | External universe/source snapshots awaiting ingestion |

Do not put the Git checkout, container layers, PostgreSQL, or active indexes on the
HDD. Those workloads generate small random reads and writes, while the corpus is
mostly sequential and append-only.

## Bulk Disk Filesystem

The Seagate ST8000DM004 8 TB HDD was reformatted from NTFS/`ntfs-3g` to native
ext4 on 2026-07-12. It is mounted by label `valuechain-bulk` with `noatime` and
uses 4 KiB blocks, 256-byte inodes, a 16 KiB bytes-per-inode ratio, 488,374,272
inodes, and 0.5% reserved blocks. The format intentionally favors a large cold
corpus containing many small documents rather than a small number of huge files.

The migration benchmark created and durably synced 20,000 1 KiB files in 1.56
seconds on ext4, compared with 214.68 seconds on NTFS/`ntfs-3g`. A 1 GiB
sequential write took 7.53 seconds on ext4, compared with 8.37 seconds on NTFS.

Loose source files are acceptable when they need independent lifecycle or direct
retrieval, but never place millions of files in one directory. Partition by
source/date or use hash-prefix fanout. Keep original SEC documents immutable,
store derived tabular data in bounded Parquet shards, and keep hashes, locations,
and provenance in PostgreSQL. Non-filing corpora belong in separate projects
with independent storage and retention policies.

## Capacity Boundary

`20,000 companies * 15 years * 20 documents/year` is 6 million documents. At an
average 1 MiB per retained document, source bytes would be about 5.7 TiB. This is
a planning scenario rather than a required reservation: actual usage depends on
retention, compression, exhibits, and whether rebuildable derivatives are kept.

Keep operational headroom as the corpus grows. A deployment that approaches the
disk boundary will eventually need one or more of:

- larger/multiple bulk disks or S3-compatible object storage;
- source-specific retention and compression policies;
- hot/cold tiers, with reproducible derivatives removable and rebuildable;
- a separate backup target. The current HDD is not a backup.

Earnings-call transcripts are not an SEC dataset. They require a separately
licensed external source and should use a distinct source registry, retention
policy, and provenance contract.

## Environment

On Cosmos, `.env` should include at least:

```dotenv
VALUECHAIN_DATA_DIR=/mnt/hdd8tb/valuechain/data
VALUECHAIN_REPORTS_DIR=/mnt/hdd8tb/valuechain/reports
VALUECHAIN_POSTGRES_DATA_DIR=/home/pi/valuechain-state/postgres
VALUECHAIN_POSTGRES_BIND=127.0.0.1
VALUECHAIN_POSTGRES_PORT=55434
VALUECHAIN_ADMINER_BIND=127.0.0.1
VALUECHAIN_ADMINER_PORT=18081
VALUECHAIN_API_BIND=100.102.250.107
VALUECHAIN_API_PORT=18018
```

Set a random PostgreSQL password and a real SEC contact identity before starting
automated collection. Keep SEC traffic globally rate-limited; worker concurrency
must not multiply the configured request rate.

## Operations

```bash
docker compose up -d postgres api
docker compose ps
curl http://100.102.250.107:18018/api/health
```

The database is authoritative for manifests, job state, provenance, and resolver
records. Raw files are immutable and should be written atomically (`.partial`,
hash/size validation, then rename). A downloader must be resumable and idempotent
before a large backfill is started.
