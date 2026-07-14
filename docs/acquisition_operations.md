# Acquisition Operations

## Scope

The first scheduled collector inventories the live SEC universe and downloads
Tier-A filings in strict descending year phases from 2026 through 2020. It
deliberately does not run parsing, relation extraction, embeddings, or LLM
calls.

Issuer order is:

1. tickers in `data/universe/ai_infra_universe.csv`;
2. NYSE, Nasdaq, and Cboe issuers;
3. remaining SEC ticker/exchange rows, including OTC.

Forms currently downloaded are 10-K, 10-Q, 8-K, 20-F, 6-K, 40-F and their
amendments. For each filing the collector retains the archive index, complete
submission text, primary document, and a hash-bearing `filing.json` manifest.

## Network Contract

All SEC requests use a normal proxy obtained from:

```text
https://proxy.frederickpi.com/proxy/random/normal
```

The asynchronous worker pool is hard-capped at eight workers. Each worker owns
its proxy and HTTP session, while all workers share one adaptive SEC rate limiter
that can start at up to eight requests per second, below SEC's global ten
requests-per-second ceiling. HTTP 429 and retryable 5xx responses
reduce the shared rate; sustained successful requests gradually restore it.
The worker rotates proxies after request failures and never falls back to a
direct SEC request. Proxy credentials are not logged or persisted.

## Storage

Raw bytes are authoritative and live on the Cosmos HDD:

```text
/mnt/hdd8tb/filings/sec_edgar/
  _catalog/company_tickers_exchange.TIMESTAMP.json
  2026/MM/CIK_PREFIX/CIK/ACCESSION/
    archive_index.json
    complete_submission.txt
    PRIMARY_DOCUMENT
    filing.json
```

Files are streamed into `.partial`, flushed, hashed, and atomically renamed.
Interrupted downloads retain the partial file and use HTTP Range requests when
the source supports them. Existing non-empty final files are treated as cache
entries and re-hashed before their manifest is rebuilt.

## Acquisition Metadata

PostgreSQL on the Cosmos NVMe is authoritative for acquisition metadata. The
tables are separate from extraction runs:

```text
acquisition_sources
acquisition_issuers
acquisition_issuer_scans
acquisition_filings
acquisition_documents
acquisition_runs
```

Uniqueness on `(source_id, source_filing_id)` and `(source_id, source_url)` makes
reruns idempotent. Queue claims use row locking with `SKIP LOCKED`, so later
bounded workers cannot claim the same issuer/year simultaneously. Per-issuer,
per-year status, retries, hashes, paths, and byte counts remain queryable without
walking the HDD.

The original SQLite checkpoint at
`/home/pi/valuechain-state/acquisition.sqlite3` is only a migration source for
the first collected batch. Raw `filing.json` manifests remain the independent
rebuild path if the metadata database is lost.

This does not commit the project to a final analytics index. Likely later layers are:

```text
PostgreSQL on NVMe
  canonical issuer/filing/document metadata, job state, provenance

Parquet on HDD
  normalized text, sections, passages, extraction batches

Full-text index on NVMe
  selected searchable filing/news text; raw bytes remain on HDD

ANN/vector index on NVMe
  passage/entity embeddings only after retrieval and deduplication stabilize
```

Do not put PostgreSQL WAL, Elasticsearch live indexes, or ANN indexes on the SMR
HDD. Snapshots and rebuildable index exports may be stored there.

## Scheduler

SEC, CNINFO, and ESEF are long-running user-level systemd services. A worker
claims 16 records at a time, processes up to four concurrently, waits one second
between non-empty batches, and uses a longer idle wait only when no work is due.
Systemd restarts a worker after an unhandled failure. GLEIF remains timer-based
because its three Golden Copy objects refresh only once per day.

```bash
systemctl --user status valuechain-sec-acquisition.service
journalctl --user -u valuechain-sec-acquisition.service -f
```

User lingering is enabled on Cosmos, so workers continue without an interactive
SSH session.

## Status and Control

```bash
cd /home/pi/ValueChain
set -a; . ./.env; set +a
.venv/bin/valuechain-acquire status
.venv/bin/valuechain-acquire migrate-sqlite

systemctl --user restart valuechain-sec-acquisition.service
systemctl --user restart valuechain-cninfo-acquisition.service
systemctl --user restart valuechain-esef-acquisition.service
```

Operational health is checked every five minutes by
`valuechain-acquisition-monitor.timer`. It verifies Postgres, queue progress,
recent raw paths, worker service state, and HDD capacity. See
`docs/ACQUISITION_MONITORING.md` for thresholds and optional webhook delivery.

The issuer universe is refreshed from the live SEC endpoint every 24 hours. The
2025 phase cannot claim an issuer until every 2026 issuer is complete. After all
configured historical phases finish, the newest year enters a 24-hour rescan
cycle so new filings continue to enter the corpus. Failed issuers use persistent
retry state rather than being silently skipped.

## Source Management

`config/filing_sources_2026.yaml` is the source onboarding queue. A source is not
enabled in the scheduler merely because an official webpage has been catalogued.
It must pass the rights, identifier, discovery, download, and completeness gates
in `docs/source_curator_instruction.md` before a source-specific worker is added.

## Parallel Global Lanes

CNINFO, priority-Europe ESEF, and GLEIF run as separate user services. They do
not share SEC's worker pool or rate budget:

```bash
systemctl --user status valuechain-cninfo-acquisition.service
systemctl --user status valuechain-esef-acquisition.service
systemctl --user status valuechain-gleif-acquisition.timer

.venv/bin/valuechain-global-acquire status
```

CNINFO claims 16 source-local issuers per batch, processes four concurrently at
an initial shared rate of two requests per second, and downloads full
annual, semiannual, first-quarter, and third-quarter reports while excluding
summary-only PDFs. Priority ESEF claims 16 filings, processes four concurrently
at an initial shared rate of four requests per second, and retains the original
report package, Inline XBRL report, and xBRL-JSON representation. Both queues
process years from 2026 through 2020 in descending order and retain retry state
in PostgreSQL.

After the CNINFO historical phases finish, the newest configured year enters a
24-hour issuer rescan cycle. ESEF refreshes its filing discovery checkpoints
every 24 hours, so both lanes continue ingesting newly published disclosures.

GLEIF has no filing year. It refreshes the latest LEI Level 1 (`lei2`), Level 2
relationship (`rr`), and reporting-exception (`repex`) Golden Copy ZIPs at most
once per 24 hours. The scheduler only downloads immutable raw files and
manifests; normalization and extraction remain separate jobs.

Raw files are written below:

```text
/mnt/hdd8tb/valuechain/global-acquisition/
```

The global lanes use `acquisition_source_checkpoints` for discovery/refresh
state and `acquisition_source_objects` for non-filing bulk objects. Files still
flow through `.partial`, fsync, hash validation, and atomic rename.
