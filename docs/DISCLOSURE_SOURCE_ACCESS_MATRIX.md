# Disclosure Source Access Matrix

Updated: 2026-07-14

## Operational classification

| Market | Canonical source | API key | Automated collection status | ValueChain lane |
| --- | --- | --- | --- | --- |
| Brazil | CVM Open Data | No | Public bulk archives | Production async downloader |
| Hong Kong | HKEXnews | No key, but permission/feed required | Public-site programmatic and systematic retrieval is restricted | Authorized package importer |
| Canada | SEDAR+ | No key, but license/negotiation required | Public terms prohibit robots, scraping, and database construction | Authorized export/feed importer plus existing alert parser |
| Australia | ASX ComNews | Commercial entitlement | Public website automation and institutional reuse are restricted | Authorized ComNews package importer |
| Germany | Unternehmensregister/Bundesanzeiger | No ordinary API key | Individual search is public; complete unattended bulk access is not a documented public API | Approved export/bulk-delivery importer and ESEF reconciliation |
| Taiwan historical | MOPS Push Server/Data E-Shop | Subscription entitlement | Historical website scraping is not enabled; current OpenAPI remains separate | Licensed package importer |

An absent API key does not imply permission to crawl or construct a database.
The repository therefore separates public machine endpoints from operator-owned
exports and licensed feeds.

## Public bulk lane: Brazil CVM

The `cvm_brazil` worker polls the official directory indexes for four series:

- `DFP`: annual standardized financial statements;
- `ITR`: quarterly information;
- `FRE`: reference forms;
- `IPE`: periodic and event disclosure metadata.

Official ZIP names are stable while their contents are revised. The queue key
therefore includes form, report year, and official index modification timestamp.
The original ZIP and a hash-bearing manifest are retained. Default discovery is
every 12 hours at 0.5 requests/second through the project proxy, one bulk object
per worker batch, with at most five transport retries.

```bash
valuechain-global-acquire run-batch --source cvm_brazil
valuechain-global-acquire run-worker --source cvm_brazil
```

## Authorized package lanes

Place an official CSV, TSV, XLS, XLSX, JSON, HTML export, licensed feed package,
or raw ZIP in the matching inbox:

```text
/mnt/hdd8tb/valuechain/official-imports/
  asx/incoming/
  hkex/incoming/
  mops/incoming/
  sedar_plus/incoming/
  unternehmensregister/incoming/
```

The importer content-addresses every package, stores it under the canonical raw
root, records it in `acquisition_source_objects`, writes a manifest, and moves
the inbox copy to `processed/`. Supported tabular exports are additionally
normalized into issuer and filing rows using source-specific aliases. Unknown
binary packages remain preserved as raw authoritative objects for a later
format-specific parser.

```bash
valuechain-global-acquire run-batch --source hkex
valuechain-global-acquire run-batch --source sedar_plus
```

The systemd template can scan an entitled source every 15 minutes:

```bash
systemctl --user enable --now valuechain-official-import@hkex.timer
```

Do not enable a timer until the corresponding feed/export delivery exists.

## Entitlements still required

- Canada: a SEDAR+ data/feed agreement or another explicitly licensed delivery.
- Australia: ASX ComNews or written authority covering automated use and storage.
- Hong Kong: written permission or a licensed HKEXnews delivery suitable for systematic collection.
- Taiwan: MOPS Push Server/Data E-Shop electronic-books package; add the XBRL package when structured reports are required.
- Germany: an approved Unternehmensregister/Bundesanzeiger export or bulk-data arrangement. Security challenges are not bypassed.

These are rights/transport dependencies, not missing parser architecture. Once
a package lands in an inbox, provenance and metadata ingestion are already wired.
