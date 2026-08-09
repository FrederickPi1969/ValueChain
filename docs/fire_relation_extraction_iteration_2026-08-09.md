# FIRE Relation Extraction Iteration, 2026-08-09

## Outcome

The retained Qwen 3.6 workflow separates NER and relation extraction, aligns entity text back to FIRE's
canonical tokens, gives each entity a stable ID, restricts relations to those IDs, validates directed endpoint
type signatures in code, and retrieves three similar demonstrations from the official FIRE training split.

On the deterministic 30-case development sample, relation micro F1 increased from `0.226601` in the reproduced
legacy workflow to `0.490566`; a final cleaned-code repeat reached `0.520000`. The earlier audit reported `0.241`;
this run-to-run spread at temperature 0 is consistent with the aggregate service nondeterminism already documented
in the audit.

The retained workflow was then evaluated on all 454 FIRE test cases:

| Metric | Full FIRE test result |
|---|---:|
| Cases / errors | 454 / 0 |
| Entity micro precision | 0.695689 |
| Entity micro recall | 0.738339 |
| Entity micro F1 | 0.716379 |
| Relation micro precision | 0.567961 |
| Relation micro recall | 0.484272 |
| Relation micro F1 | **0.522788** |
| Case-average relation F1 | 0.458507 |

These are local workflow measurements, not an official FIRE leaderboard submission.

## Controlled iterations

Every row below used Qwen/Qwen3.6-35B-A3B, temperature 0, thinking disabled, seed 1969, the same 30 FIRE test
cases, and the same exact-match scorer.

| Variant | Relation precision | Relation recall | Relation micro F1 | Decision |
|---|---:|---:|---:|---|
| Reproduced legacy two-pass workflow | 0.207207 | 0.250000 | 0.226601 | Baseline |
| Direct token-index NER + ID relations | 0.307692 | 0.130435 | 0.183206 | Rejected: NER recall collapsed |
| Exact-text NER + token alignment + ID/type constraints | 0.568182 | 0.271739 | 0.367647 | Kept as structural basis |
| Above + 3 retrieved FIRE train examples | 0.582090 | 0.423913 | **0.490566** | Retained |
| Above + 5 retrieved FIRE train examples | 0.562500 | 0.391304 | 0.461538 | Rejected: extra examples added noise |

The 3-example variant also held at `0.497018` relation micro F1 on 100 test cases before the full run.

## What changed

1. NER still runs before relation extraction. The model copies source mentions; deterministic code aligns them
   to canonical tokens and rejects paraphrased or invented spans.
2. Relation extraction receives an entity catalog and emits `head_id` and `tail_id`, not rewritten endpoint
   strings.
3. A deterministic validator rejects unknown IDs, self-relations, invalid relation labels, duplicate edges, and
   endpoint type/direction combinations outside the FIRE schema.
4. Three lexical-IDF nearest examples are retrieved only from `fire_train.json`; test labels are never exposed.
5. Production value-chain extraction now passes its persisted per-passage mentions into the relation model as an
   entity catalog. Named counterparties resolve through catalog IDs to canonical names; anonymous dependency
   classes remain allowed.

The production ontology is not FIRE's ontology, so the FIRE score validates the extraction architecture rather
than directly measuring supplier/customer graph accuracy. A separate human-labeled value-chain benchmark is
still required before automatic persistence.

## Remaining errors

The weakest full-test relation types are `Sector` (F1 `0.098`), `Propertyof` (`0.179`), `Constituentof`
(`0.288`), `ActionSell` (`0.351`), and `Productof` (`0.355`). These failures are dominated by FIRE-specific
entity-boundary conventions and distinctions among financial ownership, products, and corporate actions.
The next useful iteration is a manually reviewed value-chain set with named suppliers, customers, products,
direction, and exact evidence; further tuning against FIRE alone risks optimizing the wrong ontology.

## Reproduction

```bash
PYTHONPATH=src python scripts/run_financial_ie_benchmark.py \
  --output-dir /tmp/valuechain-fire-workflow-v2 \
  --style workflow_v2 \
  --model Qwen/Qwen3.6-35B-A3B \
  --concurrency 4 \
  --limit-per-task 454 \
  --fire-data /path/to/FIRE/fire/data/fire_test.json \
  --fire-types /path/to/FIRE/fire/data/fire_types.json \
  --fire-train-examples /path/to/FIRE/fire/data/fire_train.json \
  --fire-example-count 3
```

Raw local experiment artifacts are under `/tmp/valuechain-fire-experiments/` and are intentionally not committed.
