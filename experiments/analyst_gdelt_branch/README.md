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

- Dependency risk: disclosed dependency evidence weighted by relation type and modality.
- Operating dependency: current-fact evidence gets more weight than boilerplate risk text.
- Chokepoint exposure: dependency objects that many companies cite.
- Concentration watch: customer concentration and named/anonymous large customer risk.
- Capex beneficiary lens: data center, power, cooling, grid, cloud, and infrastructure buildout.
- Fragility lens: high forward-looking / hypothetical risk mix.
- Evidence quality: named counterparties and current-fact passages are preferred over generic classes.

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
  factor_leaderboard.csv
  analyst_summary.json
  analyst_report.md
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

Large validation run:

```bash
python experiments/analyst_gdelt_branch/scripts/fetch_gdelt_news.py \
  --tickers NVDA,AMD,TSM,ASML,AMAT,ANET,CSCO,DELL,SMCI,VRT,ETN,CEG,VST,NEE,EQIX,DLR,AMZN,MSFT,GOOGL,META \
  --start 2026-04-01 \
  --end 2026-05-18 \
  --max-records 60 \
  --concurrency 4 \
  --min-interval 0.25 \
  --timeout 15 \
  --retries 2 \
  --query-modes company,value_chain,sec_object \
  --objects-per-company 2 \
  --output experiments/analyst_gdelt_branch/outputs/gdelt_news_large/gdelt_articles_large.jsonl

python experiments/analyst_gdelt_branch/scripts/fetch_gdelt_news.py \
  --tickers NEE \
  --start 2026-04-01 \
  --end 2026-05-18 \
  --max-records 40 \
  --concurrency 1 \
  --min-interval 1.2 \
  --timeout 20 \
  --retries 3 \
  --query-modes company,value_chain,sec_object \
  --objects-per-company 2 \
  --output experiments/analyst_gdelt_branch/outputs/gdelt_news_large/gdelt_articles_nee_retry.jsonl

python experiments/analyst_gdelt_branch/scripts/merge_gdelt_jsonl.py \
  --inputs \
    experiments/analyst_gdelt_branch/outputs/gdelt_news_large/gdelt_articles_large.jsonl \
    experiments/analyst_gdelt_branch/outputs/gdelt_news_large/gdelt_articles_nee_retry.jsonl \
  --output experiments/analyst_gdelt_branch/outputs/gdelt_news_large/gdelt_articles_large_merged.jsonl
```

Local LLM event framing:

```bash
python experiments/analyst_gdelt_branch/scripts/classify_gdelt_events_llm.py \
  --input experiments/analyst_gdelt_branch/outputs/gdelt_news_large/gdelt_articles_annotated.jsonl \
  --output experiments/analyst_gdelt_branch/outputs/gdelt_news_large/event_frames_llm.jsonl \
  --limit 140 \
  --per-ticker 8 \
  --concurrency 4
```

The classifier uses the Local LLM aggregate endpoint on Endeavor:

```text
http://192.168.50.18:31969/v1
model: Qwen/Qwen3.5-4B
chat_template_kwargs.enable_thinking=false
```

Outputs:

```text
experiments/analyst_gdelt_branch/outputs/gdelt_news/
  gdelt_articles.jsonl
  gdelt_company_summary.csv
  gdelt_theme_summary.csv
  gdelt_summary.json

experiments/analyst_gdelt_branch/outputs/gdelt_news_large/
  gdelt_articles_large_merged.jsonl
  gdelt_articles_annotated.jsonl
  gdelt_company_summary.csv
  gdelt_theme_summary.csv
  gdelt_query_mode_summary.csv
  event_frames_llm.jsonl
  event_frames_llm.summary.csv

experiments/analyst_gdelt_branch/outputs/combined/
  company_thesis_monitor_large.csv
  company_thesis_monitor_large.summary.json
```

## Guardrails

- No Postgres writes.
- No production dashboard coupling.
- No SEC pipeline mutation from this folder.
- Treat GDELT as narrative/news context, not evidence for SEC-derived dependency edges.
- Use SEC evidence as the source of truth for dependency graph records.
