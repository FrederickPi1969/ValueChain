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

OpenDART's official error-code documentation says status `020` is generally
returned after at least 20,000 requests, while explicitly warning that a key can
have a different configured threshold. The production worker therefore uses a
10,000-request daily hard budget in the `Asia/Seoul` timezone, reserves one unit
before every real HTTP attempt (including retries), and stops before the next
request when the budget is exhausted. Its default transport limit is 1 request
per second with at most two workers. The defaults can only be reduced at runtime:

```dotenv
OPENDART_API_KEY=...
VALUECHAIN_OPENDART_DAILY_REQUEST_BUDGET=10000
VALUECHAIN_OPENDART_REQUESTS_PER_SECOND=1.0
VALUECHAIN_OPENDART_CONCURRENCY=2
VALUECHAIN_OPENDART_DISCOVERY_LOOKBACK_DAYS=3
VALUECHAIN_OPENDART_DISCOVERY_REFRESH_HOURS=1
VALUECHAIN_OPENDART_UNIVERSE_REFRESH_HOURS=168
```

OpenDART discovery queries the whole market by filing date with 100 rows per
page. It does not spend one request per issuer. PostgreSQL records each daily API
attempt, discovered filing, download state, retry, and document hash. Raw
corporation-code snapshots and original disclosure ZIP packages are stored under
`VALUECHAIN_GLOBAL_RAW_DIR/opendart` on the HDD.

## Operations

```bash
docker compose up -d postgres api
docker compose ps
curl http://100.102.250.107:18018/api/health
```

## Cloudflare Tunnel

The public API hostname uses a dedicated locally-managed Cloudflare Tunnel:

```text
hostname: fintelligence.frederickpi.com
tunnel: b2ca88b6-fa7e-4edd-9caa-2dd93d20d8b3
DNS CNAME target: b2ca88b6-fa7e-4edd-9caa-2dd93d20d8b3.cfargotunnel.com
origin: http://100.102.250.107:18018
```

The Cosmos connector runs as the user service
`valuechain-cloudflared.service`. Its tunnel-specific credential is stored at
`/home/pi/.cloudflared/b2ca88b6-fa7e-4edd-9caa-2dd93d20d8b3.json` with mode
`0600`; the Cloudflare account-level `cert.pem` is not copied to Cosmos.

Deploy or refresh the non-secret config and unit:

```bash
install -m 600 deploy/cloudflare/fintelligence.yml ~/.cloudflared/fintelligence.yml
install -m 644 deploy/systemd/valuechain-cloudflared.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now valuechain-cloudflared.service
```

Check the connector and public hostname:

```bash
systemctl --user status valuechain-cloudflared.service
curl https://fintelligence.frederickpi.com/api/health
```

The API container mounts SEC and global-acquisition HDD roots read-only. Set
`VALUECHAIN_FILE_API_TOKEN`, `VALUECHAIN_FILE_HOST_SEC_ROOT`, and
`VALUECHAIN_FILE_HOST_VALUECHAIN_ROOT` in the uncommitted Cosmos `.env` before
recreating the API container. File API operations are documented in
`docs/acquisition_file_api.md`.

The database is authoritative for manifests, job state, provenance, and resolver
records. Raw files are immutable and should be written atomically (`.partial`,
hash/size validation, then rename). A downloader must be resumable and idempotent
before a large backfill is started.
