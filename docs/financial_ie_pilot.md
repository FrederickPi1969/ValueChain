# No-Database Financial IE Pilot

## Scope

This pilot extracts a deliberately small first-step schema from the latest annual SEC filing for 100 companies. It is an audit artifact, not a production database load.

The universe is the 79-company AI infrastructure list plus 21 strategically important companies spanning consumer technology, finance, energy, industrials, aerospace and defense, healthcare, retail, and logistics.

## Output Schema

### Identity and provenance

- ticker, company name, CIK
- source, form, filing date, report date, accession number
- primary document URL and local HDD path
- parser version, model, warnings, latency, and raw model output

### Company profile

- business summary
- finite primary-industry classification
- strategic domains
- value-chain roles
- products and services
- end markets
- operating geographies
- strategic importance score from 1 to 5 and rationale
- filing evidence references

### Deterministic financial facts

- revenue
- net income
- operating income
- total assets and liabilities
- stockholders' equity
- cash and equivalents
- operating cash flow
- capital expenditure
- research and development
- employees when reported as inline XBRL

Values come from inline XBRL tags and consolidated annual/instant context selection. The LLM is not asked to transcribe or calculate these values.

### Material signals

- demand and revenue
- pricing and margin
- capital allocation
- capacity and supply
- customer concentration
- supplier or infrastructure dependency
- regulatory or geopolitical exposure
- technology and product transition
- partnership or M&A
- liquidity and balance sheet

Each signal carries direction, modality, significance, confidence, exact evidence quote, chunk, section, accession, and SEC URL. Unsupported quotes remain visible with `needs_evidence_review` and are not silently accepted.

## Long-Document Workflow

```text
latest annual filing manifest (catalog GET only)
  -> primary SEC HTML on Cosmos HDD
  -> deterministic section parsing
  -> paragraph chunks with stable provenance
  -> section-aware multi-query retrieval
  -> profile extraction pass
  -> material-signal extraction pass
  -> finite-schema validation
  -> exact evidence validation
  -> JSONL/CSV audit artifacts
```

The run is asynchronous with concurrency 4 and appends one completed company record at a time. Reruns skip completed tickers. It neither calls ad hoc acquisition fallback nor writes PostgreSQL.

## Run

On Cosmos:

```bash
cd /home/pi/ValueChain
PYTHONPATH=src uv run python scripts/run_financial_ie_pilot.py \
  --output-dir /mnt/hdd8tb/valuechain/audits/financial_ie/pilot-100-20260715 \
  --target-count 100 \
  --catalog-base-url http://100.102.250.107:18018 \
  --catalog-token "$VALUECHAIN_FILE_API_TOKEN" \
  --concurrency 4
```

Artifacts:

- `filing_manifest.jsonl`
- `company_records.jsonl`
- `financial_facts.jsonl`
- `material_signals.jsonl`
- `coverage.csv`
- `review_sample.csv`
- `profile_review_sample.csv`
- `quality_issues.csv`
- `run_summary.json`

`review_sample.csv` balances 50 failed and 50 validated signal citations. `profile_review_sample.csv`
contains one row per company. Both include blank `human_label` and `review_notes` columns for the next audit.
No production schema should be finalized until these samples are reviewed.

## 2026-07-15 Pilot Result

The first audit run completed 100 of 100 requested companies using the latest locally cataloged annual filing.
It produced 922 deterministic financial facts and 772 LLM-extracted material signals. No company had zero
signals; GOOGL was the only company with fewer than five. No database writes or ad hoc downloads occurred.

Core fact coverage was 99 of 100 companies for revenue, net income, total assets, and operating cash flow.
IBM is the exception: its primary 10-K HTML contains only a small set of inline numeric facts, while the annual
financial statements are in an exhibit that was not present in the local catalog. The pipeline leaves those four
facts missing instead of guessing them.

Strict citation validation passed 626 of 772 signal quotes (81.09%) and 339 of 400 profile quotes (84.75%).
Among failed citations, 143 signal quotes and 58 profile quotes were not exact spans in the cited chunk; three
in each group referenced an unknown chunk. A failed exact-quote check does not prove the claim is false, but it
does prevent the claim from being treated as verified. The review CSV deliberately oversamples these failures.

The automated audit emitted 256 issues across 84 companies: 85 errors and 171 warnings. Important known
classes include hypothetical risk language labeled as current fact, unverified high-significance evidence,
four parser warnings, and two partial JSON recoveries. These are audit findings, not production acceptance
metrics. Human review of the supplied samples remains the next gate.

For catalog coverage, the 100-company set uses ConocoPhillips in place of Exxon Mobil because the locally
available Exxon annual-filing identity mapped to a newer CIK without a usable annual filing. This substitution
keeps the pilot at 100 complete source documents but must not be mistaken for complete issuer coverage.
