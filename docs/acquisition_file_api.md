# Acquisition File API

The acquisition API exposes PostgreSQL acquisition metadata and immutable raw
files without accepting filesystem paths from clients. Production is bound to
the Cosmos Tailscale address and protects every `/api/acquisition/*` route with
`VALUECHAIN_FILE_API_TOKEN`.

## Filing Library Frontend

The production service serves a Vite frontend from the API root:

```text
https://fintelligence.frederickpi.com/
```

Use the **Filing Library** tab to search the local disclosure inventory by
source, year, company/issuer, filing id, native form, and status. Company lookup
uses server-side typeahead against `/api/acquisition/issuers` with a small
result limit, so the browser never renders the full 8,000+ issuer registry.
Selecting a filing opens its document list and authenticated original-file
downloads. The frontend stores the file API token in browser local storage and
sends it as a Bearer token to the same `/api/acquisition/*` endpoints documented
below.

API documentation remains available at:

```text
https://fintelligence.frederickpi.com/docs
```

## Endpoints

```text
GET  /api/acquisition/sources
GET  /api/acquisition/universe-policy
GET  /api/acquisition/issuers
GET  /api/acquisition/filings
GET  /api/acquisition/filings/{source_id}/{filing_id}
GET  /api/acquisition/documents
GET  /api/acquisition/documents/{document_id}/download
HEAD /api/acquisition/documents/{document_id}/download
GET  /api/acquisition/snapshots
GET  /api/acquisition/snapshots/{snapshot_id}/download
HEAD /api/acquisition/snapshots/{snapshot_id}/download
GET  /api/acquisition/objects
GET  /api/acquisition/objects/{source_id}/{object_key}/download
HEAD /api/acquisition/objects/{source_id}/{object_key}/download
```

`universe-policy` exposes the machine-readable company coverage contract:
complete US and mainland-China universes plus the Global Strategic 1000,
regional and sector allocations, strategic scoring, S/A/B monitoring tiers,
quarterly updates, identity deduplication, retention, and storage assumptions.

`documents` are filing-scoped files such as SEC primary documents, complete
submissions, archive indexes, CNINFO PDFs, and EDINET/OpenDART packages.
`objects` are source-level packages such as CVM annual ZIPs, Companies House
daily accounts ZIPs, GLEIF Golden Copies, and Taiwan snapshots.
`snapshots` are versioned issuer-universe catalogs such as SEC ticker maps and
CVM company registries.

All list endpoints are paginated. `filings` supports source, issuer, form,
status, year, and text filters. `documents` supports source, filing, status, and
SHA-256 filters. `objects` supports source, object type, status, and filing year.

## Authentication

Use either header form:

```bash
curl -H "Authorization: Bearer $VALUECHAIN_FILE_API_TOKEN" \
  http://100.102.250.107:18018/api/acquisition/sources

curl -H "X-API-Key: $VALUECHAIN_FILE_API_TOKEN" \
  'http://100.102.250.107:18018/api/acquisition/filings?source_id=sec_edgar&year=2026&limit=10'
```

An empty token disables route authentication for local development. Production
must set a random token in its uncommitted `.env`.

## Downloads

List documents or objects first and follow the returned `download_url`:

```bash
curl -H "X-API-Key: $VALUECHAIN_FILE_API_TOKEN" \
  -OJ 'http://100.102.250.107:18018/api/acquisition/documents/123/download'

curl -H "X-API-Key: $VALUECHAIN_FILE_API_TOKEN" \
  -H 'Range: bytes=0-1048575' \
  'http://100.102.250.107:18018/api/acquisition/objects/cvm_brazil/DFP%3A2026%3A20260712T0713/download' \
  -o first-megabyte.bin
```

Responses include `ETag`, `X-Checksum-SHA256`, `Accept-Ranges`,
`Content-Disposition`, and `Content-Length`. Starlette serves valid Range
requests as `206 Partial Content`.

## Filesystem Boundary

The API container mounts only these production roots, read-only:

```text
/mnt/hdd8tb/filings/sec_edgar
/mnt/hdd8tb/valuechain
```

The download handler resolves symlinks and requires the resulting regular file
to remain under an allowed root. Missing files return 404, incomplete queue
records return 409, and paths outside the allowlist return 403. API responses do
not expose `local_path` or downloader error text.
