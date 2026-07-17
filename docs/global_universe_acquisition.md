# Global Company Universe Acquisition

## Scope

This layer answers which public-company and statutory-disclosure entities are
in scope before filing extraction begins. It deliberately keeps source-local
identifiers. Cross-source canonicalization remains a later GLEIF/LEI resolver
step and never overwrites raw source records.

## Modules

- `gcu.models`: typed source, entity, filing, document, and monitor records.
- `gcu.http`: polite HTTP client, host rate limiting, retries, cache, and the
  `proxy.frederickpi.com` rotating proxy pool.
- `gcu.registry`: 78-source base catalog and shared adapters.
- `gcu_priority_markets`: 21 priority-source contracts, seven machine adapters,
  official-export workflows, monitoring, and size tiering.
- `valuechain.global_universe_store`: PostgreSQL imports and immutable snapshot
  provenance.

The priority adapters currently provide direct machine workflows for CNINFO,
KRX KIND, TMX issuer lists, NSE, BSE, FCA FIRDS, and ESEF/filings.xbrl.org.
Other markets retain explicit official-export or credential requirements rather
than silently substituting unofficial vendor data.

## Storage Contract

Raw downloads and normalized CSV/JSONL snapshots live below the HDD data root.
PostgreSQL remains on SSD and stores:

- `acquisition_sources`: authority, access mode, capabilities, and endpoint contract;
- `acquisition_issuers`: source-local issuer key, ticker, name, exchange, and identifiers;
- `acquisition_filings`: source-local filing discovery and download state;
- `acquisition_universe_snapshots`: local path, SHA-256, source URL, row count, and timestamps.

The `(source_id, source_issuer_id)` and `(source_id, source_filing_id)` keys make
imports idempotent. A rerun updates metadata but does not duplicate rows.

## Commands

Use the proxy pool for live requests when it is enabled:

```bash
export VALUECHAIN_ACQUISITION_USE_PROXY=true
export VALUECHAIN_PROXY_POOL_URL=https://proxy.frederickpi.com
valuechain-global smoke --live --source cninfo --source krx_kind --source tmx_issuer_lists
```

Set `VALUECHAIN_ACQUISITION_USE_PROXY=false` to run direct from the host network.

Generate and import a universe snapshot:

```bash
valuechain-global universe \
  --source cninfo \
  --output-csv /mnt/hdd8tb/valuechain/data/universe/cninfo/entities.csv

valuechain-global sync-universe \
  --source cninfo \
  --input-csv /mnt/hdd8tb/valuechain/data/universe/cninfo/entities.csv
```

Import discovered filing metadata only after its issuer universe is present:

```bash
valuechain-global sync-filings \
  --source cninfo \
  --input-jsonl /mnt/hdd8tb/valuechain/data/filings/cninfo/discovered.jsonl
```

## Acceptance Rules

1. Offline contract tests pass for every catalog source.
2. Direct adapters reject zero-row results and implausibly small live universes.
3. KRX decoding accepts its real EUC-KR workbook response.
4. TMX parsing detects the real delayed header and excludes funds, bonds, CDRs,
   and other non-operating-company products.
5. CNINFO records normalize venue labels to ISO MIC values (`XSHG`, `XSHE`, `XBSE`).
6. Every imported snapshot has a SHA-256 and row count.
7. Exact duplicate source keys are collapsed with an audit count; conflicting
   companies sharing one source key fail the import.
8. Raw/normalized files are never committed to Git.

Live endpoints can change without notice. A passing parser test establishes
contract compatibility, not permanent source availability; monitor row-count
drift and retain source snapshots for reconciliation.
