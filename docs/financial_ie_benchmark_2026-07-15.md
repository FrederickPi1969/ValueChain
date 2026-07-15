# Financial IE Benchmark Audit, 2026-07-15

## Decision

Qwen 3.5 4B is not reliable enough to act as an unconstrained financial information extractor. Qwen 3.6 35B is materially better, but it also should not write relation or narrative claims directly into the production database. The acceptable first step is a layered system:

1. deterministic iXBRL/XBRL extraction for standard financial facts;
2. section-aware retrieval and compact evidence windows for narrative extraction;
3. constrained JSON schemas and finite taxonomies;
4. calculator execution for arithmetic programs;
5. exact evidence validation and a candidate review queue before persistence.

## Benchmarks

The audit intentionally combines different failure surfaces instead of reporting one blended score.

| Benchmark | Credibility and task | Sample used |
|---|---|---:|
| [FinBen, NeurIPS 2024 Datasets and Benchmarks](https://proceedings.neurips.cc/paper_files/paper/2024/hash/adb1d9fa8be4576d28703b396b82ba1b-Abstract-Datasets_and_Benchmarks_Track.html) | Broad financial LLM benchmark; FLARE NER and FNXL numeric concept extraction subsets | 30 NER + 20 FNXL |
| [FIRE, NAACL 2024 Findings](https://aclanthology.org/2024.findings-naacl.230/) | Joint financial entity and directed relation extraction with 13 entity and 18 relation types | 30 |
| [FinQA, EMNLP 2021](https://aclanthology.org/2021.emnlp-main.300/) | Expert-authored numerical reasoning over financial-report text and tables | 30 |
| [FinanceBench](https://github.com/patronus-ai/financebench) | Open-book questions over complete public-company filings with evidence annotations | 30 metrics-generated questions |

[REFinD, SIGIR 2023](https://refind-re.github.io/) was reviewed as a relevant financial relation benchmark but was not run because its official data requires a separate CodaLab registration. It must not be implied that this experiment measured REFinD performance.

## Protocol

- Model endpoints: `Qwen/Qwen3.5-4B` and `Qwen/Qwen3.6-35B-A3B` through the Endeavor Local LLM aggregate service.
- Inference: temperature 0, thinking disabled, maximum concurrency 4.
- Sampling: deterministic seed `1969`; FinQA and FinanceBench use round-robin sampling across operation/reasoning groups.
- Total: 140 cases per complete run. FNXL is capped at 20 because its prompts contain the full 100-label candidate catalog.
- Direct and structured FinanceBench runs receive benchmark evidence. They measure reading/reasoning after retrieval, not end-to-end document QA.
- Workflow FinanceBench receives complete PDFs averaging 532 chunks. It must retrieve evidence before answering.
- Workflow FIRE uses one entity pass followed by a relation pass constrained to extracted entity endpoints.
- Micro F1 aggregates true/false positives and negatives across cases. Case-average F1 remains in each `summary.json` for diagnosis.
- `harness_answer_correct` executes the model's arithmetic expression when one is valid; otherwise it uses the model answer. This tests a concrete production policy, not raw model accuracy.
- These are controlled subset results, not official leaderboard scores and not confidence intervals for the complete benchmark distributions.

## Results

| Task / metric | 4B direct | 35B direct | 35B structured | 35B workflow |
|---|---:|---:|---:|---:|
| FinBen NER micro F1 | 0.035 | 0.222 | 0.593 | 0.642 |
| FinBen FNXL micro F1 | 0.188 | 0.489 | 0.536 | 0.630 |
| FIRE relation micro F1 | 0.000 | 0.115 | 0.194 | 0.241 |
| FinQA model answer accuracy | 0.467 | 0.700 | 0.667 | 0.633 |
| FinQA calculator-aware accuracy | 0.467 | 0.700 | 0.633 | 0.633 |
| FinanceBench model answer accuracy | 0.400 | 0.533 | 0.533 | 0.600 |
| FinanceBench calculator-aware accuracy | 0.400 | 0.533 | 0.767 | 0.767 |

For full-document FinanceBench, lexical evidence-page hit@8 was `0.867`, hybrid/faceted hit@8 was `0.900`, hit@12 was `0.967`, and citation-page hit was `0.933`. Exact page matching can undercount valid alternative passages, so answer accuracy remains the decision metric.

An immediately preceding workflow repeat produced NER `0.634`, FNXL `0.667`, FIRE relation `0.293`, FinQA calculator-aware `0.667`, and FinanceBench calculator-aware `0.667`. This observed spread at temperature 0 is retained as deployment nondeterminism, not averaged away or replaced with the best run.

## Findings

1. Model scale matters, but 35B alone does not solve ontology extraction. Direct FIRE relation micro F1 is only `0.115`.
2. Task definitions and constrained schemas matter more than generic prompting for NER: 35B micro F1 rose from `0.222` to `0.593`.
3. Splitting entity and relation extraction raised FIRE relation micro F1 to `0.241` in the final run (`0.293` in the prior repeat), still far below an auto-persist threshold.
4. Deterministic numeric-token candidates improved FNXL substantially, confirming that token alignment should be a tool operation rather than an LLM responsibility.
5. Long-document retrieval was initially the dominant FinanceBench failure. Query decomposition into financial line-item facets raised page hit@8 from `0.50` in the first workflow smoke to `0.90`.
6. Calculator execution can repair arithmetic rendering but cannot repair a semantically wrong numerator, denominator, period, or sign.
7. Temperature 0 did not make repeated aggregate-service runs bit-identical. Production evaluation must retain raw responses and prompt hashes.

## Failure Cases

- NER benchmark conventions can label defined legal party roles differently from ordinary semantic NER. Without explicit conventions, exact-match scores collapse.
- FNXL one-shot output confused value tokens with adjacent currency/unit tokens and used inconsistent indexes.
- FIRE errors include omitted non-name entities, wrong relation ontology, wrong direction, and relations whose endpoints do not match exact spans.
- FinQA errors commonly selected the wrong comparison denominator even when all source numbers were present.
- Long filings contain repeated metrics in notes and narrative sections; semantic similarity alone can select the wrong period or table row.
- A model may produce many valid array objects and then hit `max_tokens`, leaving the outer JSON invalid. The pilot limits result count, stores raw output, and recovers only fully decoded objects while flagging partial recovery.
- Evidence quotes can differ only because inline SEC HTML splits `22%` into separate text nodes. Validation normalizes such layout artifacts but does not accept paraphrases as citations.

## Representative Cases

- FIRE `fire:625`: the workflow correctly extracted both `investment in real estate -> $2.2 billion` and
  `debt -> $1.4 billion` as `ValueChangeIncreaseby` relations. Relation F1 was 1.0, although it still missed
  the benchmark's `three` quantity entity and added one extra action entity.
- FIRE `fire:1097`: it returned no entities or relations for the statement that raw material, labor, and
  overhead constitute inventory cost. Gold contains four entities and three `Constituentof` relations.
- FinanceBench `financebench_id_04209`: full-document retrieval found Costco's balance sheet and the model
  returned FY2021 total assets of `$59,268 million` with the correct page citation.
- FinanceBench `financebench_id_02981`: retrieval found Corning's correct income-statement row, but the model
  pooled three years of operating income and revenue instead of averaging the three annual margins. The model's
  written answer also disagreed with its own expression. Calculator execution exposed the inconsistency but could
  not repair the semantically wrong formula.

These examples are retained in the gitignored prediction JSONL; they are not selected to claim best-case
performance. They illustrate distinct success, omission, retrieval, and reasoning failure modes.

## Reproduction

The runner is `scripts/run_financial_ie_benchmark.py`. A representative workflow command is:

```bash
PYTHONPATH=src uv run --with pyarrow python scripts/run_financial_ie_benchmark.py \
  --output-dir reports/financial_ie/workflow-v3-35b-n30 \
  --style workflow \
  --model Qwen/Qwen3.6-35B-A3B \
  --concurrency 4 \
  --limit-per-task 30 \
  --finben-ner /tmp/flare-ner-test.parquet \
  --finben-fnxl /tmp/flare-fnxl-test.parquet \
  --fire-data /tmp/valuechain-bench-fire/fire/data/fire_test.json \
  --fire-types /tmp/valuechain-bench-fire/fire/data/fire_types.json \
  --finqa /tmp/valuechain-bench-finqa/dataset/test.json \
  --financebench /tmp/valuechain-bench-financebench/data/financebench_open_source.jsonl \
  --financebench-pdfs /tmp/valuechain-bench-financebench/pdfs
```

The benchmark datasets and PDFs are intentionally not committed. Input SHA-256 values for this run are:

```text
e33ad8fa9d15f8d244a0c058964a4d027d362381428e739205d7017c663aa607  flare-ner-test.parquet
997bcb6b3ea1408683e4607d1c4213a03cc71f03234a3e71579319e121fec3db  flare-fnxl-test.parquet
87f59e4ec9844961fda348214f0861abdcce40c091356174a27a38ef0f035c2b  fire_test.json
831dbfb2e785dbc227f895ce3f24046433467aec67b09db2bd6ac7692a8a30dc  FinQA test.json
a5a2aa673e573e55675fc3c0f9aa38c1cf59d2abc91edb077534f71f10a71877  financebench_open_source.jsonl
```

Raw predictions and summaries are under `reports/financial_ie/` and remain gitignored.
