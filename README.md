# Disclosure-Derived AI Value Chain Graph Prototype

This repository is a compact prototype for turning SEC filings into typed,
evidence-backed dependency records for AI infrastructure companies. The first
goal is to run an industry-level value-chain workflow over a controlled universe,
not to produce a perfect global industry graph on day one.

## What It Builds

The pipeline follows the project shape in the brief:

1. universe definition
2. SEC identifier bootstrap
3. filing discovery
4. raw archive download
5. section parsing
6. passage segmentation
7. relevance filtering
8. entity mention extraction
9. entity resolution
10. relation evidence extraction
11. evidence-to-edge aggregation
12. dashboard / validation output

The current extractor is deliberately modest:

- rules mode is deterministic and runs without an LLM;
- hybrid / LLM modes can call an OpenAI-compatible Qwen endpoint;
- every record keeps evidence text, SEC accession, section, passage id, parser
  version, extractor version, confidence, modality, and archive URL.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
cd frontend && npm install && cd ..
```

Edit `.env` and set `VALUECHAIN_SEC_USER_AGENT` to a real project/contact
string before larger SEC runs.

## Input Surface

The default input is the AI infrastructure universe in
`data/universe/ai_infra_universe.csv`, not a single company. Use `plan` before a
large run:

```bash
valuechain universe --priority 1
valuechain plan --priority 1 --forms 10-K,10-Q,8-K --max-filings-per-company 3 --write
```

Useful input controls:

- `--priority 1` keeps the highest-priority value-chain names first.
- `--roles cloud_hyperscaler,foundry,data_centers` runs specific chain layers.
- `--tickers NVDA,AMD,MSFT` is for debugging or focused review.
- `--limit-companies 10` caps a batch while keeping deterministic ordering.
- `--filing-date-from YYYY-MM-DD` and `--filing-date-to YYYY-MM-DD` bound the SEC filing window.
- `--forms 10-K,10-Q,8-K,20-F,6-K` controls disclosure types.
- `--max-filings-per-company` controls depth per issuer.

The plan output includes company count, role coverage, forms, filing upper
bound, and a conservative SEC request estimate. Archive downloads are cached
under `data/raw/`, so reruns skip already-downloaded primary filing documents.

## Quick Run

Small SEC-only smoke run:

```bash
source .env
valuechain run --tickers NVDA,AMD,MSFT --forms 10-K,10-Q,8-K --max-filings-per-company 2 --skip-yahoo
```

Industry-layer run over priority 1 names, with a named run for the frontend:

```bash
valuechain run --priority 1 --forms 10-K,10-Q,8-K,20-F --max-filings-per-company 3 --skip-yahoo --run-label "Priority 1 AI infra"
```

With Yahoo Finance enrichment:

```bash
valuechain run --priority 1 --max-filings-per-company 2
```

Hybrid extraction, using the configured Qwen endpoint when available and
falling back to rules if an LLM call fails:

```bash
valuechain run --tickers NVDA,AMD,MSFT --extractor hybrid --llm-concurrency 8 --max-filings-per-company 1
```

Optional embedding-assisted object merge uses the same Endeavor aggregate
OpenAI-compatible endpoint and `qwen3-embed-0.6b`:

```bash
valuechain run --priority 1 --forms 10-K --max-filings-per-company 1 --embedding-merge --embedding-threshold 0.92
```

GLEIF-backed entity normalization is available as a resolver candidate queue.
It does not overwrite graph edges. It takes extracted object strings, queries
the official GLEIF API for LEI reference data and fuzzy legal-name matches, and
writes reviewable candidates with confidence, jurisdiction, mapped identifiers,
and relationship links:

```bash
valuechain resolve-entities \
  --run-id industry-sec-exhibits-v3 \
  --limit-objects 100 \
  --min-evidence-count 2 \
  --max-candidates 5
```

Outputs are written next to the run artifacts:

- `entity_resolution_candidates.csv`
- `entity_resolution_candidates.jsonl`
- `entity_resolution_candidates.summary.json`

Add Local LLM adjudication when you want a best-match review layer on top of
GLEIF candidates:

```bash
valuechain resolve-entities \
  --run-id industry-sec-exhibits-v3 \
  --limit-objects 100 \
  --min-evidence-count 2 \
  --max-candidates 5 \
  --llm-select \
  --llm-concurrency 4
```

This writes:

- `entity_resolution_llm_selected.csv`
- `entity_resolution_llm_selected.jsonl`
- `entity_resolution_llm_selected.summary.json`

The LLM selector only chooses among GLEIF candidates or returns `no_match` /
`ambiguous`; it does not create new LEIs and does not overwrite graph edges.

Generate a company-level dependency brief as a separate reporting layer:

```bash
python scripts/generate_company_dependency_brief.py \
  --run-id industry-sec-exhibits-v3 \
  --company NVDA
```

This uses `Qwen/Qwen3.6-35B-A3B` by default for the analyst interpretation and
writes Markdown/JSON under `reports/runs/<run_id>/briefs/`. The deterministic
brief builder reads existing evidence, graph, filing, and GLEIF-selected entity
artifacts; it does not mutate the pipeline outputs or Postgres. The report
writer is multi-round: outline planning, final writing, citation validation, and
repair when needed. See `docs/company_dependency_brief.md`.

Sync generated briefs into the Vite dashboard's static data and open the
frontend Briefs tab:

```bash
python scripts/sync_company_briefs_to_frontend.py --run-id industry-sec-exhibits-v3
cd frontend && npm run dev
```

For a focused smoke test:

```bash
valuechain resolve-entities \
  --objects "NVIDIA Corporation,Taiwan Semiconductor Manufacturing Company Limited,Microsoft Corporation" \
  --output-dir data/processed/gleif_smoke
```

`VALUECHAIN_HTTP_PROXY` / `VALUECHAIN_HTTPS_PROXY` can be used for SEC and LLM
HTTP calls when the network path requires `proxy.frederickpi.com`.

Outputs are written to:

- `data/processed/runs/<run_id>/company_universe_resolved.csv`
- `data/processed/runs/<run_id>/filing_manifest.csv`
- `data/processed/runs/<run_id>/passages.jsonl`
- `data/processed/runs/<run_id>/candidate_passages.jsonl`
- `data/processed/runs/<run_id>/relation_evidence_raw.jsonl`
- `data/processed/runs/<run_id>/relation_evidence.jsonl`
- `data/processed/runs/<run_id>/merge_diagnostics.csv`
- `data/processed/runs/<run_id>/graph_edges.csv`
- `data/processed/runs/<run_id>/validation_sample.csv`
- `data/processed/runs/<run_id>/run_summary.json`
- `reports/runs/<run_id>/dashboard-data.json`
- `frontend/public/data/runs/<run_id>/dashboard-data.json`

## Vite Frontend

The main frontend is a Vite React app in `frontend/`. It reads the FastAPI
backend first when it is running, and falls back to generated JSON artifacts in
`frontend/public/data` for offline review.

```bash
cd frontend
npm run dev
```

Open:

```text
http://127.0.0.1:5173/
```

Each `valuechain run ...` writes an independent run under:

- `data/processed/runs/<run_id>/`
- `reports/runs/<run_id>/`
- `frontend/public/data/runs/<run_id>/dashboard-data.json`

`reports/dashboard.html` remains only a legacy latest-run static snapshot.

The Vite frontend follows a Seeking Alpha-style research console: dense tables,
schema-level exposure views, and evidence drill-down instead of a raw graph as
the primary interface. It includes:

- global search across companies, dependencies, relation types, and evidence;
- company / relation / modality filters;
- filtered metrics for companies, edges, current evidence, risk evidence, and bottleneck candidates;
- relation and modality mix bars;
- company x relation heatmap;
- portfolio exposure, bottleneck, edge, and evidence tabs;
- evidence drawer with SEC provenance and source filing link;
- CSV export for the filtered edge table.

## Postgres

Start local Postgres and the async API:

```bash
docker compose up -d postgres api
```

Write a run into Postgres:

```bash
valuechain run --priority 1 --limit-companies 5 --forms 10-K --max-filings-per-company 1 --skip-yahoo --run-id pg-smoke-5 --run-label "Postgres smoke" --write-postgres
```

Default connection:

```text
postgresql://valuechain:valuechain_dev@127.0.0.1:5433/valuechain
```

The backend API is available at:

```text
http://127.0.0.1:8000/api/health
http://127.0.0.1:8000/api/runs
http://127.0.0.1:8000/api/runs/<run_id>/dashboard-data
http://127.0.0.1:8000/api/runs/<run_id>/edges
http://127.0.0.1:8000/api/runs/<run_id>/evidence
```

Acquisition metadata and raw files are exposed separately under
`/api/acquisition`. These routes support source, issuer, filing, document, and
bulk-object queries plus authenticated Range downloads. See
[`docs/acquisition_file_api.md`](docs/acquisition_file_api.md).

For local development without Docker:

```bash
VALUECHAIN_DATABASE_URL="postgresql://valuechain:valuechain_dev@127.0.0.1:5433/valuechain" valuechain-api
```

Optional DB browser:

```bash
docker compose up -d adminer
```

Adminer is available at `http://127.0.0.1:8081` with server `postgres`,
database `valuechain`, user `valuechain`, password `valuechain_dev`.

## Design Notes

This is aimed at ETF portfolio managers, so the first dashboard is not a pure
network graph. It emphasizes:

- which public companies disclose dependency pressure;
- dependency type and modality;
- evidence count and confidence;
- bottleneck candidates that appear as repeated dependency objects;
- market context from Yahoo Finance when available.

The ontology is intentionally small and editable in `config/ontology.yaml`.
The source registry is in `config/source_registry.yaml`.

The 2026-first global acquisition audit is tracked separately from extraction:

- `config/filing_sources_2026.yaml` is the ranked machine-readable source queue;
- `docs/filing_download_plan_2026.md` defines the US-first download and completeness plan;
- `docs/source_curator_instruction.md` is the source-research assignment and acceptance rubric.

The scheduled SEC acquisition worker is operationally separate from extraction.
See `docs/acquisition_operations.md` for its proxy, storage, checkpoint, systemd,
and status procedures.

Global company-universe acquisition is exposed separately through
`valuechain-global`. It combines the migrated base source catalog with the
priority-market adapters without coupling global issuer discovery to relation
extraction:

```bash
valuechain-global doctor
valuechain-global sources
valuechain-global smoke --offline
valuechain-global universe --source cninfo --output-csv /mnt/hdd8tb/valuechain/data/universe/cninfo.csv
valuechain-global sync-universe --source cninfo --input-csv /mnt/hdd8tb/valuechain/data/universe/cninfo.csv
valuechain-global database-status
```

Normalized snapshots remain on HDD. PostgreSQL stores source-local issuer keys,
identifiers, source definitions, filing discovery state, snapshot hashes, and
row counts. See `docs/global_universe_acquisition.md` for source coverage,
acceptance checks, and import operations.

The graph build separates raw extraction from graph-ready evidence. Raw records
are written to `relation_evidence_raw.jsonl`; schema-aware denoising
canonicalizes aliases, removes generic placeholder objects that are not graph
ready, and writes the audit trail to `merge_diagnostics.csv`. Bottleneck ranking
excludes generic dependency classes such as `supplier(s)` or
`cloud or hosting provider`, so repeated placeholders do not masquerade as real
industry chokepoints.

## Async LLM Extraction

LLM and hybrid extraction use an async OpenAI-compatible client with connection
pooling and a semaphore-controlled request limit. The default extraction route
uses Endeavor's Local LLM aggregate proxy directly,
`http://192.168.50.18:31969/v1`, with `Qwen/Qwen3.5-4B`; the larger 35B model
remains configured separately for later complex steps. The default concurrency is conservative:

```bash
VALUECHAIN_LLM_CONCURRENCY=4
```

Raise it per run only when the endpoint can handle the load:

```bash
valuechain run --priority 1 --extractor hybrid --llm-concurrency 8 --max-filings-per-company 1 --write-postgres
```

## Unified Disclosure API

The local-first cross-market report resolver, canonical document taxonomy,
exact native form mappings, request parameters, and ad hoc fallback behavior are
documented in `docs/UNIFIED_DISCLOSURE_API.md`.
