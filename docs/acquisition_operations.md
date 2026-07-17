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

Scheduled workers use the rotating proxy pool by default:

```text
https://proxy.frederickpi.com/proxy/random/normal
```

Set `VALUECHAIN_ACQUISITION_USE_PROXY=false` to run acquisition workers direct
from the host network. Keep `VALUECHAIN_PROXY_POOL_URL` configured so proxy mode
can be re-enabled without redeploying code.

The asynchronous worker pool is hard-capped at eight workers. Each worker owns
its HTTP session and, in proxy mode, its proxy. All workers share one adaptive
SEC rate limiter that can start at up to eight requests per second, below SEC's
global ten requests-per-second ceiling. HTTP 429 and retryable 5xx responses
reduce the shared rate; sustained successful requests gradually restore it. In
proxy mode the worker rotates proxies after request failures. Proxy credentials
are not logged or persisted.

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

SEC, CNINFO, ESEF, OpenDART, EDINET, TWSE, TPEx, and Companies House bulk are
long-running user-level systemd services. A worker
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
systemctl --user restart valuechain-opendart-acquisition.service
systemctl --user restart valuechain-edinet-acquisition.service
systemctl --user restart valuechain-twse-acquisition.service
systemctl --user restart valuechain-tpex-acquisition.service
systemctl --user restart valuechain-companies-house-bulk-acquisition.service
systemctl --user restart valuechain-cvm-bulk-acquisition.service
```

Operational health is checked every five minutes by
`valuechain-acquisition-monitor.timer`. It verifies Postgres, queue progress,
recent raw paths, worker service state, and HDD capacity. See
`docs/ACQUISITION_MONITORING.md` for thresholds and optional webhook delivery.

The SEC issuer universe is refreshed from the live endpoint every 24 hours.
CNINFO's combined SSE/SZSE/BSE issuer map is also refreshed every 24 hours. Each
source keeps the current calendar year on a rolling 24-hour issuer rescan even
while older years are being backfilled. Due current-year scans take priority;
when that incremental queue is fresh, the same worker resumes the descending
historical queue. Failed issuers use persistent retry state rather than being
silently skipped.

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
systemctl --user status valuechain-opendart-acquisition.service
systemctl --user status valuechain-edinet-acquisition.service

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

CNINFO refreshes its issuer denominator and current-year filing scans every 24
hours while historical phases continue. ESEF refreshes its filing discovery
checkpoints every 24 hours, so both lanes continue ingesting newly published
disclosures.

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

## Taiwan OpenAPI Lanes

TWSE and TPEx run as independent source-scoped listeners at a default 0.5
requests per second through the project proxy. Each listener refreshes the
official listed-company universe and twelve current financial-statement table
shapes every 24 hours. The table shapes cover balance sheets and income
statements for general industry, banking, securities, financial holding,
insurance, and miscellaneous regulated templates.

The official material-event endpoint is checked hourly. Its full response is
saved as an immutable hash-addressed snapshot, while each newly observed event
is also stored as a company-scoped JSON document and a `material_event` filing
row. The event document retains the original row, subject, explanation, event
date, announcement time, snapshot path, and snapshot hash. This makes the lane
directly usable by later relevance and relation extraction without treating a
current API response as historical completeness.

These listeners do not yet constitute a historical MOPS report backfill. They
provide current company normalization, structured financial snapshots, and a
rolling high-value disclosure feed.

## Companies House Accounts Bulk Lane

`companies_house_accounts_bulk` uses the public daily accounts product and
does not require `COMPANIES_HOUSE_API_KEY`. It refreshes the official index
every six hours, records each dated ZIP as a PostgreSQL source object, and
claims the newest uncollected date first. Only one large ZIP is claimed per
batch by default. Downloads use the project proxy, at most five retries,
adaptive rate limiting, `.partial` files, HTTP Range resume, ZIP signature
validation, SHA-256, and a sidecar manifest.

This lane stores raw accounts packages only. Extraction of individual iXBRL or
XBRL members is intentionally a downstream job so raw acquisition remains
restartable and independent of parser versions. Older monthly archives are a
separate future backfill lane and are not implied by the daily listener.

## CVM And Authorized Disclosure Lanes

Brazil CVM DFP, ITR, FRE, and IPE ZIP archives use a public, version-aware bulk
worker. HKEX, SEDAR+, ASX, German Unternehmensregister, and historical MOPS use
the separate official-package importer because their public websites do not
provide unrestricted institutional crawling rights. See
`docs/DISCLOSURE_SOURCE_ACCESS_MATRIX.md` for inbox paths and entitlement gates.

## Curated Korea And Japan Lanes

OpenDART and EDINET are deliberately watchlist collectors, not full-market raw
mirrors. Their versioned catalogs are:

```text
config/curated_markets/korea.csv   89 issuers
config/curated_markets/japan.csv   92 issuers
```

The catalogs prioritize major companies, globally relevant technology and
industrial supply chains, and exporters. Tier and theme columns are retained in
issuer and filing metadata. Editing the CSV changes scope without changing the
transport or database code.

Both listeners make one market-level daily discovery request (paginated where
the authority requires it), then filter before any issuer or filing is stored.
This is materially cheaper than polling every company separately and ensures
that domestic small-company records never enter the queue.

Discovery and downloads are recent-first. The current year takes precedence
over historical years, discovery walks backward from the latest date, and each
year's filing queue claims `filing_date DESC, source_filing_id DESC`. Completed
documents remain idempotent and are never downloaded again when ordering changes.

OpenDART refreshes the corporation-code master weekly using resumable transfer,
maps the Korean ticker catalog to corporation codes, and downloads only original
disclosure packages for matched companies. Its internal safety ceiling is 10,000
attempts per Asia/Seoul day at no more than 1 request per second and two workers.
OpenDART status `020` immediately exhausts the local budget for that day.

EDINET retains selected issuer registration, annual, quarterly, semiannual, and
extraordinary reports plus their amendments. It excludes fund reports,
confirmation reports, internal-control reports, large-holder reports, and
withdrawn documents. It downloads the original type-1 XBRL submission package.
The FSA does not publish a fixed universal numeric quota in its API materials;
the worker therefore uses a conservative local ceiling of 1,000 attempts per
Asia/Tokyo day, 0.5 requests per second, and two workers. HTTP 429 and retryable
server failures reduce the adaptive request rate and rotate the project proxy.
