# Analyst + GDELT Exploration Branch

This folder is a local experiment branch, not a Git branch and not a production pipeline.
It deliberately avoids Postgres writes. Scripts read existing run artifacts and write only
under this folder's `outputs/` directory unless an explicit output path is supplied.

## Goal

Explore whether the SEC-derived dependency graph can become a better investor-facing
analysis product by adding:

1. Analyst-style modeling beyond a raw knowledge graph.
2. GDELT news coverage overlays for event and narrative monitoring.

The intended reader is closer to a Seeking Alpha / ETF analyst than a graph database user:
they need thesis screens, risk monitors, bottleneck ranking, and evidence drill-downs.

## Direction A: Analyst Modeling Beyond KG

The current graph answers "who depends on whom." That is necessary but not enough for
financial analysis. This branch experiments with higher-level lenses:

- Dependency intensity: how many disclosed dependencies a company has, by type.
- Modality mix: current operating fact vs. risk hypothetical vs. forward-looking language.
- Chokepoint exposure: whether a company depends on objects that many other companies
  also disclose as dependencies.
- Concentration watch: customer concentration and named/anonymous large customer risk.
- Capex beneficiary lens: companies exposed to data center, power, cooling, grid, and
  infrastructure buildout themes.
- Fragility lens: high ratio of risk-hypothetical dependency language to current-fact
  operating relationships.

Run:

```bash
python experiments/analyst_gdelt_branch/scripts/build_analyst_lens.py \
  --run-id industry-sec-exhibits-v3
```

Outputs:

```text
experiments/analyst_gdelt_branch/outputs/analyst_lens/<run_id>/
  company_scorecard.csv
  bottleneck_thesis.csv
  analyst_summary.json
```

## Direction B: GDELT News Overlay

The GDELT flow follows the QuackQuackQuant pattern:

- GDELT Doc 2.0 endpoint: `https://api.gdeltproject.org/api/v2/doc/doc`
- mode: `ArtList`
- per-company keyword queries
- async concurrent fetch with retry and polite throttling
- JSONL/CSV local outputs

Smoke fetch:

```bash
python experiments/analyst_gdelt_branch/scripts/fetch_gdelt_news.py \
  --tickers NVDA,AMD,CEG,DLR \
  --start 2026-05-01 \
  --end 2026-05-07 \
  --max-records 50 \
  --concurrency 2
```

Summarize:

```bash
python experiments/analyst_gdelt_branch/scripts/summarize_gdelt_news.py \
  --input experiments/analyst_gdelt_branch/outputs/gdelt_news/gdelt_articles.jsonl
```

Outputs:

```text
experiments/analyst_gdelt_branch/outputs/gdelt_news/
  gdelt_articles.jsonl
  gdelt_company_summary.csv
  gdelt_theme_summary.csv
  gdelt_summary.json
```

## Guardrails

- No Postgres writes.
- No production dashboard coupling.
- No SEC pipeline mutation from this folder.
- Treat GDELT as narrative/news context, not evidence for SEC-derived dependency edges.
- Use SEC evidence as the source of truth for dependency graph records.

