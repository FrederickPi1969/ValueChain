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

Industry-layer run over priority 1 names:

```bash
valuechain run --priority 1 --forms 10-K,10-Q,8-K,20-F --max-filings-per-company 3 --skip-yahoo
```

With Yahoo Finance enrichment:

```bash
valuechain run --priority 1 --max-filings-per-company 2
```

Hybrid extraction, using the configured Qwen endpoint when available and
falling back to rules if an LLM call fails:

```bash
valuechain run --tickers NVDA,AMD,MSFT --extractor hybrid --max-filings-per-company 1
```

`VALUECHAIN_HTTP_PROXY` / `VALUECHAIN_HTTPS_PROXY` can be used for SEC and LLM
HTTP calls when the network path requires `proxy.frederickpi.com`.

Outputs are written to:

- `data/processed/company_universe_resolved.csv`
- `data/processed/filing_manifest.csv`
- `data/processed/passages.jsonl`
- `data/processed/candidate_passages.jsonl`
- `data/processed/relation_evidence.jsonl`
- `data/processed/graph_edges.csv`
- `data/processed/yahoo_snapshot.csv`
- `data/processed/validation_sample.csv`
- `data/processed/run_summary.json`
- `data/processed/input_plan.json`
- `reports/dashboard.html`

Open `reports/dashboard.html` in a browser to inspect company exposures,
bottleneck candidates, typed edges, and original filing evidence.

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
