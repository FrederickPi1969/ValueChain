# Unified Disclosure Resolution API

The acquisition API exposes one cross-market request contract while retaining
each authority's native form name/code. Resolution is local first:

1. resolve the company inside the selected source;
2. return complete local documents when present;
3. on a miss, queue a legal and configured on-demand connector;
4. the worker downloads the original into HDD raw storage and records the filing,
   document path, source URL, SHA-256, size, and retrieval timestamp;
5. the API returns the document metadata and authenticated download URL.

The file-serving API remains read-only. `valuechain-ad-hoc-acquisition.service`
is the component that writes raw storage and holds upstream credentials.

Interactive OpenAPI documentation is served at:

```text
http://100.102.250.107:18018/docs
```

Use the Swagger **Authorize** button with either `AcquisitionApiKey`
(`X-API-Key`) or `AcquisitionBearer`. ReDoc is available at `/redoc`, and the
machine-readable contract is `/openapi.json`.

## Request

`POST /api/acquisition/resolve`

```json
{
  "source_id": "sec_edgar",
  "company": "NVDA",
  "company_identifier": "ticker",
  "year": 2025,
  "document_type": "annual_report",
  "source_document_type": "10-K",
  "year_basis": "auto",
  "include_amendments": false,
  "allow_fallback": true,
  "wait_seconds": 30
}
```

| Parameter | Required | Meaning |
| --- | --- | --- |
| `company` | yes | CIK, ticker, source issuer id, corp_code, EDINET code, LEI, or exact legal name. |
| `source_id` | recommended | Source registry id. Omit only for a globally unambiguous company identifier. |
| `company_identifier` | no | `auto`, `source_issuer_id`, `ticker`, or `legal_name`; default `auto`. |
| `year` | yes | Fiscal/report year for periodic reports; filing year for event reports. |
| `document_type` | yes | Canonical cross-market type listed below. |
| `source_document_type` | no | Exact native form/name/code, such as `20-F`, `120`, or `사업보고서`. |
| `year_basis` | no | `auto`, `report_period`, or `filing_date`. |
| `include_amendments` | no | Include amended/corrected filings; default `false`. |
| `allow_fallback` | no | Permit upstream retrieval after a local miss; default `true`. |
| `wait_seconds` | no | Wait 0-120 seconds for the worker; zero returns `202` and a status URL. |

`GET /api/acquisition/schema` returns the machine-readable parameter JSON
Schema, every source mapping, identifier type, credential requirement, and
fallback mode. `GET /api/acquisition/requests/{request_id}` polls a queued job.

## Canonical document types

- `annual_report`
- `quarterly_report`
- `semiannual_report`
- `current_report`
- `material_event`
- `earnings_release`
- `annual_financial_statements`
- `interim_financial_statements`
- `reference_form`
- `registration_statement`
- `other_regulatory_filing`

## Source mappings

| `source_id` | Region | Company identifiers | Canonical type -> native name/code | Miss behavior |
| --- | --- | --- | --- | --- |
| `sec_edgar` | United States / foreign SEC issuers | CIK, ticker, legal name | annual -> `10-K`, `20-F`, `40-F` and `/A`; quarterly -> `10-Q` and `/A`; current -> `8-K`, `6-K` and `/A` | `on_demand`: submissions API + EDGAR archive |
| `cninfo` | Mainland China | org_id, ticker, legal name | annual -> `annual_report`/年度报告; semiannual -> `semiannual_report`/半年度报告; quarterly -> `q1_report`, `q3_report`, `quarterly_report` | `on_demand`: company/year disclosure query |
| `opendart` | Korea | corp_code, stock code, legal name | annual -> 사업보고서; semiannual -> 반기보고서; quarterly -> 분기보고서; event -> 주요사항보고서 | `on_demand`: corp_code/date API; `OPENDART_API_KEY` and quota required |
| `edinet` | Japan | EDINET code, security code, legal name | registration -> `030/040/060/070/080/090`; annual -> `120/130`; quarterly -> `140/150`; semiannual -> `160/170`; event -> `180/190` | `scheduled_bulk`: API lists the whole market one date at a time |
| `priority_eu_esef` | selected EEA/UK ESEF | LEI, legal name | annual -> ESEF/iXBRL filing package | `scheduled_bulk`; discovery layer is not a completeness authority |
| `twse`, `tpex` | Taiwan current feeds | ticker, legal name | event -> `material_event` | `current_only`; historical MOPS reports are separate |
| `cvm_brazil` | Brazil | CVM code, CNPJ, ticker, legal name | annual statements -> `DFP`; interim -> `ITR`; reference form -> `FRE`; events -> `IPE` | `scheduled_bulk` official ZIPs |
| `companies_house_accounts_bulk` | United Kingdom | company number, legal name | annual statements -> `accounts` | `scheduled_bulk` daily accounts ZIPs |
| `hkex` | Hong Kong | stock code, legal name | Annual Report, Interim Report, Quarterly Report, Announcement | `authorized_import_only` |
| `sedar_plus` | Canada | profile number, ticker, legal name | Annual financial statements, Interim financial report, Annual information form, Material change report | `authorized_import_only` |
| `asx` | Australia | ticker, legal name | Annual Report, Half Yearly Report, Market Announcement | `authorized_import_only` via ComNews/authority |
| `mops` | Taiwan historical | ticker, legal name | annual, quarterly, material event packages | `authorized_import_only` |
| `unternehmensregister` | Germany | LEI, register id, legal name | ESEF annual report | `authorized_import_only` |

`authorized_import_only` is deliberate. An absent API key does not imply a
right to scrape a website or build a systematic archive. These sources return a
structured capability error on a local miss rather than bypassing terms or
security controls.

## Responses

- `200 complete`, `retrieval=local`: local cache hit.
- `200 complete`, `retrieval=upstream_api`: worker finished during `wait_seconds`.
- `202 queued/discovering/downloading/retry`: poll `status_url`.
- `404 company_not_found`: issuer registry could not resolve the company.
- `404 local_miss_no_realtime_fallback`: fallback was disabled.
- `409 ambiguous_company`: specify source/id type using returned candidates.
- `409 local_miss_no_realtime_fallback`: source is scheduled, current-only, or authorized-import-only.

Every returned document includes its source filing id, native form, filing and
report dates, original URL, archive URL, content type, byte size, SHA-256, and
`download_url`.
