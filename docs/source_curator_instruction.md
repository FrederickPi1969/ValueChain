# Filing Source Curator Instruction

## Objective

Turn one catalog entry into an evidence-backed acquisition contract. Do not build
a production scraper first. The curator must determine whether the source can be
used legally, reproducibly, and completely for filings dated from 2026-01-01.

## Assignment Input

Each assignment must specify:

```text
source_id:
jurisdiction:
authority/operator:
official starting URL:
target issuer population:
target document classes:
curator:
reviewer:
```

Use `config/filing_sources_2026.yaml` as the assignment queue, in ascending
`rank` order after SEC.

## Required Investigation

### 1. Authority and Rights

- Confirm that the operator is the regulator, appointed storage mechanism,
  exchange, registry, or an explicitly labeled fallback.
- Save links to terms of use, robots policy, API agreement, rate limits, license,
  and redistribution restrictions.
- State separately whether we may automate access, retain raw bytes, run NLP,
  store derived data, and redistribute identifiers/snippets/documents.
- Never bypass CAPTCHA, authentication, anti-bot controls, or undocumented access
  restrictions.

### 2. Issuer Denominator

- Locate the official listed-company or filer universe.
- Record native issuer ID, security ID, ticker, ISIN/LEI when available, exchange,
  listing status, and effective dates.
- Record whether funds, debt issuers, delisted companies, foreign issuers, and
  multiple share classes are included.
- Provide the official row count and snapshot timestamp.

### 3. Filing Discovery

- Identify the supported date query, issuer query, pagination, update frequency,
  historical lower bound, and correction/amendment semantics.
- Preserve the source-native filing ID and raw document category.
- Build a raw-to-normalized form mapping; do not discard the raw label.
- Determine an independent denominator for completeness reconciliation.

### 4. Document Retrieval

- Determine how to retrieve the primary document, attachments, XBRL/iXBRL,
  original filing package, PDF, and corrected versions.
- Record redirects, content types, compression, filename rules, language, and
  whether URLs expire.
- Confirm whether one filing can contain multiple issuers or documents.

### 5. Operational Contract

- Record authentication, quota, rate limit, concurrency limit, timeout behavior,
  maintenance windows, and support contact.
- Estimate 2026 issuer count, filing count, document count, and byte volume.
- Define retryable and terminal errors.

## Required Sample

Use at least five issuers:

```text
1 large domestic issuer
1 mid-cap issuer
1 small issuer
1 foreign or dual-listed issuer when available
1 delisted/corrected/amended edge case
```

For each issuer, capture filings from at least two 2026 dates and at least two
document classes. Save only the minimum sample allowed by the source terms.

## Deliverables

Create the following package:

```text
curation/sources/{source_id}/
  source.yaml
  access_and_rights.md
  form_mapping.csv
  issuer_sample.csv
  filing_sample.csv
  coverage_report.json
  failure_cases.md
  reproduction.md
```

`source.yaml` must contain:

```yaml
source_id: example
as_of_date: YYYY-MM-DD
authority: ""
official_urls: []
access_mode: public_api|public_bulk|official_web|api_key|licensed|blocked
credential_env_vars: []
automation_allowed: unknown
raw_storage_allowed: unknown
derived_storage_allowed: unknown
redistribution_allowed: unknown
issuer_denominator: ""
historical_start: ""
discovery_method: ""
document_method: ""
rate_limit: ""
completeness_denominator: ""
status: research|blocked|candidate|accepted
blockers: []
```

Every sample row must include source URL, source-native IDs, retrieval timestamp,
HTTP status, content type, size, and SHA-256. Never commit API keys, cookies,
licensed documents, or high-volume data.

## Acceptance Criteria

The reviewer marks a source `accepted` only when:

1. rights for the intended use are explicit;
2. the issuer denominator and filing denominator are reproducible;
3. 2026 date filtering and pagination are demonstrated;
4. sample documents download without browser-only manual intervention;
5. amendments/corrections and identifier semantics are documented;
6. all sample hashes and provenance fields validate;
7. unexplained sample gaps are zero;
8. production rate and storage estimates are supplied.

Use `blocked` rather than inventing an endpoint when licensing, terms, CAPTCHA,
or missing credentials prevent acceptance. A source homepage is not an API, a
successful sample is not historical completeness, and a global fallback is not a
canonical national filing source.
