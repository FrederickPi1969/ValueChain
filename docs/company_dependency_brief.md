# Company Dependency Brief Generator

This module generates a portfolio-manager friendly company brief from existing
run artifacts. It is intentionally separate from the extraction pipeline:

```text
data/processed/runs/{run_id}/relation_evidence.jsonl
data/processed/runs/{run_id}/graph_edges.csv
data/processed/runs/{run_id}/company_universe_resolved.csv
data/processed/runs/{run_id}/entity_resolution_llm_selected.csv
        -> company_dependency_brief module
        -> Markdown + JSON brief
```

The module does not mutate relation evidence, graph edges, Postgres tables, or
entity-resolution output. It is a presentation/reporting layer.

## Output Structure

Each generated brief contains:

1. Company role
2. Top operating dependencies
3. Top risk exposures
4. Current-fact edges
5. Strategic relations
6. Evidence table with SEC provenance
7. Analyst interpretation

The deterministic layer builds the claims and evidence table. The LLM layer only
generates the analyst interpretation.

## LLM Model

For report generation, use the complex Local LLM model:

```text
Qwen/Qwen3.6-35B-A3B
```

Calls go through the Endeavor aggregate OpenAI-compatible endpoint configured by
`VALUECHAIN_LLM_BASE_URL`, which defaults to:

```text
http://192.168.50.18:31969/v1
```

The request keeps `chat_template_kwargs.enable_thinking=false` through the shared
`OpenAICompatibleClient`, because the output is strict JSON.

## Command

```bash
python scripts/generate_company_dependency_brief.py \
  --run-id industry-sec-exhibits-v3 \
  --company NVDA
```

Outputs:

```text
reports/runs/industry-sec-exhibits-v3/briefs/NVDA_dependency_brief.md
reports/runs/industry-sec-exhibits-v3/briefs/NVDA_dependency_brief.json
```

Use deterministic fallback without LLM:

```bash
python scripts/generate_company_dependency_brief.py \
  --run-id industry-sec-exhibits-v3 \
  --company NVDA \
  --no-llm
```

## Design Notes

- Current operating claims are filtered to `current_fact` and a confidence floor.
- Risk-hypothetical language is presented under risk exposure, not current
  operating dependency.
- Selected GLEIF LLM matches are used only to display canonical object names and
  LEIs when available.
- Generic dependency classes are kept as recall signals but ranked below named
  legal entities in operating dependency sections.
