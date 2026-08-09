# FIRE Relation Extraction Iteration, 2026-08-09

## Outcome

The retained Qwen 3.6 workflow separates NER and relation extraction, aligns entity text back to FIRE's
canonical tokens, gives each entity a stable ID, marks those IDs in the relation input, restricts relations to
those IDs, validates directed endpoint type signatures in code, and retrieves three BM25-nearest demonstrations
from the official FIRE training split.

The scorer was corrected in this iteration to match FIRE's strict relation contract: both endpoint token spans,
both endpoint entity types, direction, and relation type must match. Under this strict scorer, the previous full-test
workflow is `0.498448` relation micro F1 rather than the earlier local text-key score of `0.522788`.

The retained workflow was then evaluated on all 454 FIRE test cases:

| Metric | Full FIRE test result |
|---|---:|
| Cases / errors | 454 / 0 |
| Entity micro precision | 0.688306 |
| Entity micro recall | 0.734785 |
| Entity micro F1 | 0.710786 |
| Relation micro precision | 0.566434 |
| Relation micro recall | 0.467436 |
| Relation micro F1 | **0.512195** |
| Case-average relation F1 | 0.449831 |

These are local workflow measurements, not an official FIRE leaderboard submission.

## Controlled iterations

Every row below used Qwen/Qwen3.6-35B-A3B, temperature 0, thinking disabled, seed 1969, the same 30 FIRE test
cases, and scorer v0.3 unless identified as a historical result.

| Variant | Relation precision | Relation recall | Relation micro F1 | Decision |
|---|---:|---:|---:|---|
| Historical text-key scorer, 3 IDF examples | 0.582090 | 0.423913 | 0.490566 | Superseded scorer |
| Strict scorer, IDF examples, no markers | 0.593750 | 0.413043 | 0.487179 | Strict baseline repeat |
| Strict scorer, IDF examples + entity markers | 0.608696 | 0.456522 | 0.521739 | Marker pilot |
| Strict scorer, BM25 examples + entity markers | 0.661290 | 0.445652 | **0.532468** | Retained pilot |
| BM25 + marker + selective NER audit | 0.655738 | 0.434783 | 0.522876 | Rejected: extra call, no gain |

The retained BM25 + marker variant scored `0.514735` strict relation micro F1 on 100 test cases and `0.512195`
on all 454 test cases.

### Oracle and ablation results

Gold-entity runs isolate relation classification from NER. They show that entity quality is the dominant ceiling:

| Gold-entity relation input | Relation micro F1 |
|---|---:|
| Entity catalog only | 0.738854 |
| Entity markers | 0.789809 |
| Legal candidate pairs | 0.785714 |
| Entity markers + legal candidate pairs | **0.828402** |

With predicted entities frozen, markers improved relation F1 from `0.484076` to `0.509804`. Candidate pairs
reduced F1 to `0.444444`, because noisy entity types create noisy legal pairs. Candidate enumeration is therefore
kept as an oracle diagnostic, not enabled in the end-to-end default.

## What changed

1. NER still runs before relation extraction. The model copies source mentions; deterministic code aligns them
   to canonical tokens and rejects paraphrased or invented spans.
2. Relation extraction receives an entity catalog and emits `head_id` and `tail_id`, not rewritten endpoint
   strings.
3. A deterministic validator rejects unknown IDs, self-relations, invalid relation labels, duplicate edges, and
   endpoint type/direction combinations outside the FIRE schema.
4. Three BM25-nearest examples are retrieved only from `fire_train.json`; test labels are never exposed.
5. Production value-chain extraction now passes its persisted per-passage mentions into the relation model as an
   entity catalog and marks those entity IDs directly in the passage. Named counterparties resolve through catalog
   IDs to canonical names; anonymous dependency classes remain allowed.
6. Production coreference is intentionally conservative: issuer pronouns map to the issuer; third-person pronouns
   map to a catalog entity only with one grammatically unambiguous local antecedent. FIRE itself excludes implied
   entities, so no FIRE score is claimed for document-level coreference.

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
  --fire-example-count 3 \
  --fire-example-strategy bm25 \
  --fire-mark-entities
```

Raw local experiment artifacts are under `/tmp/valuechain-fire-experiments/` and are intentionally not committed.
