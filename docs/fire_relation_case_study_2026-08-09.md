# FIRE relation case study, 2026-08-09

## Conclusion

The current Qwen 3.6 workflow is not primarily limited by relation direction or relation-label selection. It is
limited by strict entity span/type quality. On the 454-case FIRE test output, 1,213 gold relations exist, but only
777 have both exact gold endpoints in the predicted entity catalog. This caps relation recall at 0.641 before the
relation model runs. Given both endpoints, Qwen recovers 566/777 relations (conditional recall 0.728).

Simple train-derived lexical rules do not close the gap. A configuration selected on the official dev split
changed dev F1 from 0.5219 to 0.5245 and test F1 from 0.5325 to 0.5353. On test it added 11 true positives and 19
false positives. The rule-only result has 0.709 precision but just 0.060 recall. This is a small reproducible gain,
not a path from the low-50s to 0.60; continuing to add regular expressions risks FIRE-specific overfitting.

## Full test error taxonomy

| Error source | Count |
|---|---:|
| False negatives with both endpoints present | 211 |
| False negatives with missing head only | 170 |
| False negatives with missing tail only | 196 |
| False negatives with both endpoints missing | 70 |
| False positives containing a false entity endpoint | 293 |
| False positives between real entities but unsupported | 47 |
| Wrong direction/reversed pair | 4 |
| Wrong relation label on the exact pair | 3 |

The 293/347 false positives involving a false entity endpoint are especially important: a relation-only rule or
prompt cannot repair them after noisy entities enter the catalog.

## Entity and relation weak points

| Entity type | Strict entity F1 |
|---|---:|
| Sector | 0.184 |
| BusinessUnit | 0.313 |
| Product | 0.448 |
| FinancialEntity | 0.604 |
| Company | 0.757 |
| Date | 0.902 |
| Person | 0.936 |
| Money | 0.960 |

The four weak semantic types participate in 602/1,213 (49.6%) gold relations. Their boundaries depend on dataset
semantics, not surface form alone. For example, FIRE may label the industry phrase but exclude a trailing word
such as `market`; it distinguishes an external Company from a BusinessUnit and an offered Product from a generic
noun phrase. These are the exact cases in which regex rules are brittle.

Relation types backed by explicit numeric/event cues already perform well: `ValueChangeDecreaseby` is 0.857,
`ValueChangeIncreaseby` 0.815, `Valuein` 0.746, and `Actionin` 0.736. The weak relations depend on the weak entity
types: `Sector` is 0.045, `Propertyof` 0.162, `Productof` 0.193, `Quantity` 0.281, and `Constituentof` 0.339.

## Representative cases

1. `fire:2613`: `critical care market` contains a Sector boundary that excludes dataset-specific surrounding
   words, while `System One` and competitor names create Product/Company/Designation ambiguity. Regex can find
   `market`, but not reliably reproduce the annotated semantic boundary.
2. `fire:2847`: one company is linked to three coordinated petroleum Product spans. Missing the company or any
   full coordinated span removes multiple `Productof` relations at once.
3. `fire:1043`: `documentation agent`, `syndication agent`, and `administrative agent` show why partial role spans
   create false `Designation` relations even when the company endpoint is correct.
4. `fire:58`: the acquisition sentence contains two Action mentions, a multi-token Company, Money, and the
   FinancialEntity `Cash And Stock`. Correct relations require exact boundaries plus distinct `Actionto`,
   `ActionBuy`, and `Value` semantics.
5. `fire:107`: both Person and Company can be present while `Employeeof` is omitted; this is one of the 211 cases
   where a better relation candidate pass can help after entity quality is fixed.

## Ablations

All rows use the same Qwen/Qwen3.6-35B-A3B relation prompt and strict scorer.

| Official dev configuration | Entity F1 | Relation precision | Relation recall | Relation F1 |
|---|---:|---:|---:|---:|
| Current end-to-end pipeline | 0.7262 | 0.6186 | 0.4514 | 0.5219 |
| Gold Sector/Product/BusinessUnit/FinancialEntity only | 0.8623 | 0.7170 | 0.5192 | **0.6023** |
| All gold entities | 1.0000 | 0.8543 | 0.6033 | **0.7072** |

The hybrid row is an oracle diagnostic, not a deployable benchmark result. It proves that improving only the four
weak entity categories is sufficient to cross 0.60 without replacing or fine-tuning the relation model.

## Recommendation

For the fastest reliable path, train or adopt a span-based NER component only for the four weak semantic types,
then retain the existing entity-ID Qwen relation extractor and deterministic signature verifier. This is much
smaller than replacing the pipeline with a jointly trained relation model.

A no-fine-tuning path remains technically possible: generate a bounded noun-phrase/span lattice, classify each
candidate type with Qwen, and adjudicate disagreements before relation extraction. However, earlier split NER,
indexed spans, free completion, relation-conditioned recovery, and consensus-verifier experiments did not improve
the 100-case score reliably. This route trades training for substantially more inference and still needs to lift
strict entity F1 from about 0.72 toward the 0.86 demonstrated by the semantic-type oracle.

The official specialized systems remain the safer route to the high-60s end-to-end FIRE range. The gold-entity
Qwen result is not directly comparable to their end-to-end score; it isolates the relation component and shows
that specialized relation training itself is not the missing capability.
