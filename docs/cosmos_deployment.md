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

## Current Disk Constraint

The 8 TB drive is a 7.3 TiB NTFS filesystem mounted through `ntfs-3g`. This is a
safe migration target, but it is not the final filesystem for a multi-million-file
Linux corpus. Before the first full historical backfill, migrate or reformat it as
XFS or ext4 after separately confirming that its existing Garage marker/data are no
longer required. Reformatting is destructive and is not part of the initial move.

Avoid one directory per passage and avoid millions of loose derived files. Keep
original SEC documents immutable, partition manifests by source/form/year/month,
and store derived tabular data in bounded Parquet shards. Hashes and provenance
belong in PostgreSQL; document bytes belong on bulk storage.

## Capacity Boundary

`20,000 companies * 15 years * 20 documents/year` is 6 million documents. At an
average 1 MiB per retained document, source bytes alone are about 5.7 TiB. The 7.3
TiB HDD cannot safely hold that source corpus plus exhibits, normalized text,
Parquet derivatives, indexes, retries, temporary files, and backups.

Keep at least 15-20% free space. A complete global deployment therefore needs one
or more of:

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
curl http://100.102.250.107:18018/health
```

The database is authoritative for manifests, job state, provenance, and resolver
records. Raw files are immutable and should be written atomically (`.partial`,
hash/size validation, then rename). A downloader must be resumable and idempotent
before a large backfill is started.
