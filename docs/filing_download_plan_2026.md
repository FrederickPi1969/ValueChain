# 2026-First Global Filing Download Plan

## Operational Coverage Snapshot (2026-07-14)

The global company denominator and raw filing corpus have different coverage.
Do not describe an imported issuer list as filing coverage.

Production raw acquisition currently includes:

| Lane | Issuers in Postgres | Raw coverage | Status |
| --- | ---: | --- | --- |
| SEC EDGAR | 7,636 | United States registrants plus SEC-reporting foreign issuers and ADRs | Enabled; 2026 through 2020 queue |
| CNINFO | 5,881 | Mainland China SSE, SZSE, and BSE financial reports | Enabled; 2026 through 2020 queue |
| Priority ESEF | 770 discovered | France, Italy, Spain, and Netherlands ESEF packages | Enabled; 2026 through 2020 discovery |
| GLEIF Golden Copy | Global | LEI Level 1/Level 2 reference objects, not filings | Enabled refresh timer |

The ESEF lane is configured for Germany as well, but the current secondary
index produced zero German filings. Germany is therefore **not covered**. It
requires reconciliation and acquisition from Unternehmensregister or another
accepted national Officially Appointed Mechanism.

The following denominators are loaded in Postgres but their filing collectors
are disabled: Japan (3,904), Korea (2,760), Hong Kong (2,812), Canada (2,138),
Australia (1,988), Taiwan TWSE/TPEx (1,980 combined), and Brazil (663). These are
company lists only.

Candidate adapters exist for EDINET, OpenDART, Companies House, CVM, and
filings.xbrl.org. Japan, Korea, and the United Kingdom currently lack their
required API credentials on Cosmos. Most remaining markets still need a live
contract test, official export workflow, access approval, or licensed feed.

## Missing Source Inputs

### Credentials We Can Obtain Immediately

1. `EDINET_API_KEY` for Japan.
2. `OPENDART_API_KEY` for Korea.
3. `COMPANIES_HOUSE_API_KEY` for the United Kingdom.

These unlock official APIs for which candidate adapters already exist in the
audited bundle. Each still requires a live 2026 contract test and reconciliation
against the relevant listed-company universe.

### Licenses or Written Permission

1. Canada SEDAR+: do not scrape the public site. Its terms prohibit automated
   scraping and database construction; arrange licensed access with the operator.
2. ASX ComNews: the complete Australian announcement feed is a subscription.
3. SGXNews: the complete Singapore XML announcement feed is licensed.
4. TWSE MOPS Push Server: obtain package and historical terms if public MOPS
   acquisition is not approved for the required scale.
5. SIX Swiss Exchange: obtain a machine-feed and retention license.
6. Earnings-call transcripts: choose a vendor and obtain storage, NLP, derived
   data, and redistribution rights. SEC 8-K exhibits are earnings releases, not
   complete spoken transcripts.

### Technical and Policy Curation

1. HKEXnews title search and document download contract.
2. SSE, SZSE, BSE, and CNINFO as four distinct China source contracts.
3. FCA National Storage Mechanism public retrieval/export automation.
4. NSE and BSE India filing acquisition and cross-exchange deduplication.
5. MOPS public document acquisition versus paid push delivery.
6. EU country-level Officially Appointed Mechanisms, especially Germany and
   Ireland. filings.xbrl.org is a fallback and explicitly incomplete.
7. JSE SENS, Saudi Exchange, MAGNA, KAP, and the remaining catalog-only markets.

## Phase 0: Acquisition Foundation

Do not start a large extraction job as part of the download backfill. Acquisition
must be independently resumable and measurable.

Required database objects:

```text
source
issuer_identity
security_listing
filing
source_document
download_attempt
coverage_partition
reconciliation_result
```

Every downloaded object needs:

```text
source_id
source_native_issuer_id
source_native_filing_id
filing_type_raw
filing_type_normalized
filed_at
published_at
source_url
retrieved_at
http_status
content_type
content_length
sha256
storage_path
parser_status
```

Downloader requirements:

- global per-source rate limiter;
- bounded asynchronous HTTP concurrency;
- `.partial` write, hash/size validation, then atomic rename;
- retry classification and exponential backoff;
- persistent queue and idempotent uniqueness constraints;
- source response and parser version provenance;
- inventory-only mode separate from byte download mode;
- no LLM calls in the acquisition layer.

## Phase 1: United States From 2026-01-01

### Universe

Refresh the live SEC `company_tickers_exchange.json`; do not use the bundle's
2026-05-29 mirror as the production baseline. Preserve every SEC CIK, then create
separate cohorts rather than deleting records:

```text
US-A: NYSE, Nasdaq, and Cboe operating companies
US-B: foreign private issuers and ADRs with SEC filings
US-C: OTC issuers
US-D: funds, trusts, SPACs, shells, and other filer classes
```

The first byte-download pass is US-A plus US-B. The complete issuer inventory is
retained so excluded cohorts remain measurable.

### Discovery and Reconciliation

Use multiple official SEC channels for different purposes:

1. nightly `submissions.zip` for bulk filer histories;
2. company Submissions JSON and its historical pagination files;
3. daily indexes for incremental discovery;
4. quarterly `master.idx` as the independent completeness denominator;
5. archive filing detail/index for attachment inventory;
6. accession number as the immutable filing key.

For every 2026 day/quarter, reconcile discovered `(CIK, accession, form)` rows
against the official index. A run is complete only when every difference is
explained as scope exclusion, amendment normalization, deleted/corrected filing,
or acquisition failure.

### Download Priority

Inventory all forms first. Download bytes in tiers:

```text
Tier A
  10-K, 10-Q, 8-K, 20-F, 6-K, 40-F and amendments
  complete-submission text
  primary document
  all archive attachment metadata
  Exhibit 10, 21, 99/99.1

Tier B
  DEF 14A, S-1, F-1, 424B*, registration and merger documents

Tier C
  remaining issuer filings and attachments
```

The current SEC client is not sufficient for this run: it reads only
`filings.recent`, downloads synchronously by company, caps selected exhibits,
writes files non-atomically, and does not reconcile daily/quarterly indexes.

### Schedule

```text
Backfill 1: 2026-01-01 through current date, US-A
Backfill 2: 2026-01-01 through current date, US-B
Backfill 3: US-C and US-D according to product scope
Incremental: daily index plus current filings, every 10-15 minutes
Reconciliation: prior day daily; prior quarter after SEC index finalization
```

## Phase 2: Major Non-US Markets

Start each market only after its source record is marked accepted:

```text
Wave 2A: Canada, UK, Hong Kong, Japan, Korea
Wave 2B: China, Taiwan, Brazil, Australia, Singapore, India
Wave 2C: EU OAMs/ESAP, Switzerland, South Africa, Saudi Arabia, Israel, Turkey
```

API-key sources can proceed once credentials and live samples pass. Licensed or
policy-restricted markets remain blocked; issuer IR pages and
filings.xbrl.org may provide labeled fallback coverage but cannot be reported as
canonical completeness.

## Storage Layout

Cold source documents belong on Cosmos ext4 storage:

```text
/mnt/hdd8tb/filings/{source_id}/{year}/{month}/{issuer_prefix}/{filing_id}/
```

Use source/date/hash-prefix directory fanout. Independent low-read source files
may remain loose files; derived facts, passages, and extraction output should be
batched into Parquet. PostgreSQL manifests, queues, and indexes stay on NVMe.

## Release Gates

A source cannot be called covered until all are true:

1. official authority and access terms are recorded;
2. issuer/listing denominator is versioned;
3. 2026 discovery is reproducible;
4. document bytes are downloadable with stable source-native IDs;
5. corrections, amendments, delistings, and pagination are handled;
6. a representative sample passes hash and provenance validation;
7. denominator reconciliation has no unexplained gaps;
8. credentials and licenses permit the intended storage and product use.
