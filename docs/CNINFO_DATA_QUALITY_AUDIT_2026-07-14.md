# CNINFO Data Quality Audit - 2026-07-14

## Scope

This audit covered all 31,933 downloaded CNINFO filings from filing years 2025
and 2026, plus a stratified sample of 58 PDFs across form, year, exchange, and
file-size extremes.

## File Integrity

- 58/58 sampled files existed at the database path.
- 58/58 matched the stored byte size and SHA-256.
- 58/58 manifests parsed and pointed to exactly one primary PDF.
- 58/58 PDFs parsed successfully; none were encrypted or empty.
- Page counts ranged from 1 to 487.
- One 91.6 MB report contained recoverable non-standard PDF object pointers.
  It still parsed as 233 pages and exposed a text layer.

## Confirmed Metadata And Coverage Problems

1. Beijing Stock Exchange coverage was empty. All 331 `XBSE` issuers had zero
   filings because CNINFO returns issuer-filtered BSE results through the
   combined `szse` search column, while the adapter sent `bse`.
2. 5,305 semiannual reports were labeled `annual_report`. The classifier
   checked the substring `年度报告` before `半年度报告`.
3. 31,821 of 31,933 filing dates were one day earlier than the Chinese
   disclosure date. CNINFO timestamps represent China-local midnight; the
   parser converted to UTC and then took the UTC calendar date.
4. A small set of English and non-standard Chinese titles fell through to
   `announcement`, including annual, semiannual, and quarterly reports.
5. English report summaries were not removed by the Chinese-only `摘要`
   filter, creating avoidable duplicate/noisy documents.
6. Three pairs of distinct announcement IDs had identical PDF hashes. These
   appear to be duplicate publications or renamed issuer disclosures and
   should remain separate provenance records while downstream aggregation
   deduplicates by content hash.

## Remediation

- Route BSE issuer searches through CNINFO's combined `szse` column.
- Classify semiannual and quarter markers before generic annual markers and
  recognize common English title variants.
- Derive `filing_date` in `Asia/Shanghai` while retaining the UTC publication
  timestamp in metadata.
- Filter Chinese and English summary/abstract titles before download.
- Requeue 2025 and 2026 BSE issuer scans and backfill existing date/form
  metadata without deleting raw files.

## Remaining Limitations

- The issuer table stores the current CNINFO company name, while filing
  metadata preserves the name used at disclosure time. ST status and later
  renames therefore require temporal entity aliases downstream.
- PDF parser tolerance is still necessary because some issuer-generated PDFs
  are structurally non-standard despite being readable.
- Content hashes should be used as a denoising signal, not as a reason to
  overwrite or discard distinct source announcement IDs.
