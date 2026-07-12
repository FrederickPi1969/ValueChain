# Acquisition Operations

## Scope

The first scheduled collector inventories the live SEC universe and downloads
Tier-A filings in strict year phases: complete the first issuer scan for 2026,
then begin 2025. It deliberately does not run parsing, relation extraction,
embeddings, or LLM calls.

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

The worker uses one proxy per issuer, rotates after request failures, has one
active issuer worker, and enforces a global SEC rate of one request per second.
The request semaphore is hard-capped at four for future bounded parallel work,
and every request adds a 0.5-second sleep plus up to 0.5 seconds of jitter. It
never falls back to a direct SEC request. Proxy credentials are not logged or
persisted.

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
Existing non-empty files are treated as cache entries and re-hashed before their
manifest is rebuilt.

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

Cosmos uses a user-level systemd timer. Each service invocation processes a
bounded issuer batch; systemd never overlaps two invocations of the same unit.

```bash
systemctl --user status valuechain-sec-acquisition.timer
systemctl --user list-timers valuechain-sec-acquisition.timer
journalctl --user -u valuechain-sec-acquisition.service -f
```

The timer starts two minutes after boot and schedules the next batch two minutes
after the prior batch exits. User lingering is enabled on Cosmos, so it continues
without an interactive SSH session.

## Status and Control

```bash
cd /home/pi/ValueChain
set -a; . ./.env; set +a
.venv/bin/valuechain-acquire status
.venv/bin/valuechain-acquire migrate-sqlite

systemctl --user stop valuechain-sec-acquisition.timer
systemctl --user start valuechain-sec-acquisition.timer
systemctl --user start valuechain-sec-acquisition.service
```

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
