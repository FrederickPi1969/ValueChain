# Multilingual Financial IE Experiment

## Scope

This is a no-database experiment for native-language regulatory disclosures. It covers only the four
languages already represented by usable local source material:

| Language | Source | Experiment document type |
| --- | --- | --- |
| Simplified Chinese (`zh-Hans`) | CNINFO | annual reports in PDF |
| Traditional Chinese (`zh-Hant`) | TWSE | material-event JSON records |
| Japanese (`ja`) | EDINET | annual and semiannual iXBRL packages |
| Korean (`ko`) | OpenDART | quarterly disclosure XML packages |

German, French, and Portuguese were intentionally deferred. Traditional Chinese periodic-report quality
is not tested because the current Taiwan corpus contains event disclosures rather than complete annual-report
documents. The experiment reads existing HDD files and never calls an acquisition API or PostgreSQL.

## Unified Schema

Schema version `multilingual-financial-ie-v0.3` keeps English field names and finite taxonomies while retaining
native-language values and evidence. English summaries are explicitly marked `model_generated_unverified`.

Identity and provenance include source, filing ID, issuer ID and name, ticker, jurisdiction, language, native
filing type, canonical filing type, filing date, source URL, local path, parser name/version, and warnings.

Profile fields include native and English business summaries, primary industry, strategic domains, value-chain
roles, native products/services, end markets, operating geographies, and native evidence quotes.

Signals include a finite category, native/English headline and statement, direction, modality, significance,
confidence, chunk, source section, exact native quote, translation status, and review status.

Relations include native/English subject and object, finite relation type, semantic direction, modality,
temporal scope, certainty, confidence, exact native evidence, semantic warning, and review status. Direction is
not JSON ordering. Examples include `subject_depends_on_object`, `subject_controls_object`,
`object_controls_subject`, `subject_invests_in_object`, and `bidirectional`.

## Workflow

```text
existing CNINFO / EDINET / OpenDART / TWSE file
  -> source-specific native text parser
  -> stable chunks and localized section hints
  -> Unicode NFKC normalization
  -> CJK/Hangul-aware BM25 retrieval
  -> profile extraction for periodic reports
  -> signal and relation extraction
  -> finite-schema normalization
  -> native exact-evidence validation
  -> conditional batched citation repair for failed quotes
  -> deterministic relation semantic guards
  -> JSONL/CSV audit artifacts only
```

CNINFO prefers `pdftotext`; Cosmos currently lacks that system binary, so the tested run used the `pypdf`
fallback and recorded a warning. EDINET parses public iXBRL HTML members. OpenDART source XML is not always
strict XML; the recovery path preserves `SECTION-2` and `TITLE` structure with an HTML parser instead of
flattening the entire report. Taiwan material events are already evidence-bearing JSON.

The tokenizer uses Latin/number tokens plus overlapping CJK or Hangul character bigrams. This avoids a first
version dependency on language-specific segmenters while retaining native financial and dependency queries.

## Extraction Rules

1. Native filing evidence is canonical; English translation is never evidence.
2. Evidence must be one contiguous source span. Table cells, rows, bullets, and sentences cannot be synthesized.
3. A signed agreement still in force is a current fact; an unsigned plan is forward-looking; conditional harm
   remains hypothetical risk.
4. A commercial purchase, sale, supply, or foundry contract is not automatically a strategic partnership.
5. Acquiring a seller's business does not make the seller a controlled subsidiary.
6. Consolidated filings retain the disclosed subsidiary as relation subject when the sentence attributes the
   relationship to that subsidiary.
7. Named relation objects must be present in evidence, except when the object is the filing issuer established
   by document context.
8. Relation type and direction must agree, and native evidence must contain a type-specific relation cue.
9. Anonymous counterparty codes, ordinal placeholders, generic control objects, and multi-entity object lists
   remain recall-preserving candidates but are not graph-ready.

## Prompt Iterations

Round 1 used eight documents. All JSON payloads parsed, but exact evidence passed for only 71 of 95 items
(74.74%). Japanese passed 62.50%. Most failures joined non-contiguous table cells or rewrote field labels.
`strategic_partner` also absorbed licensing and investment relations.

Round 2 added exclusive relation definitions, a significance rubric, stricter contiguous-quote instructions,
and one conditional citation-repair call per affected document. On the four worst documents, exact evidence rose
to 47 of 49 items (95.92%); Japanese rose to 92.59%.

Version 0.3 added finite relation direction and deterministic graph-readiness guards. It preserves all raw
candidates and lowers only their review status. This is intentionally recall-first at extraction and stricter at
edge promotion.

## Final 16-Document Audit

The final set contains four documents per language and spans 16 issuers/documents. It produced 68 signals and
57 relation candidates.

| Metric | Overall | zh-Hans | zh-Hant | ja | ko |
| --- | ---: | ---: | ---: | ---: | ---: |
| Documents complete | 16/16 | 4/4 | 4/4 | 4/4 | 4/4 |
| Signals | 68 | 22 | 8 | 19 | 19 |
| Relation candidates | 57 | 21 | 7 | 16 | 13 |
| Exact native evidence | 97.66% | 100% | 100% | 91.84% | 100% |
| Graph-ready after guards | 20 | 8 | 2 | 5 | 5 |

The exact-evidence rate is not extraction accuracy. It only says that a cited source-language span can be found.

A deterministic six-signals-per-language sample was manually reviewed for category, modality, headline support,
and evidence support. Eighteen of 24 were fully correct (75%). The dominant failures were category errors,
modality errors, and headlines that asserted more than the cited span.

All 20 graph-ready relations were manually reviewed for subject scope, type, direction, modality, and evidence.
Eighteen were fully correct (90%). Both failures were consolidated-subsidiary scope errors: procurement disclosed
for SDC and Harman was incorrectly attributed to Samsung Electronics. These results are small-sample estimates,
not benchmark confidence intervals.

## Failure Cases

- Tables invite the model to join cells and create a readable but nonexistent quote.
- Exact quote matching does not prove that a quote supports every number or adjective in a headline.
- R&D can be incorrectly classified as capital allocation instead of technology/product.
- Current mitigation activity can be mislabeled forward-looking because its objective is future-oriented.
- A parent issuer can incorrectly inherit a consolidated subsidiary's operating relationship.
- Multi-company lists need deterministic splitting before entity resolution.
- Event disclosures can support event facts but cannot substitute for a periodic company profile.

## Run

On Cosmos:

```bash
cd /home/pi/ValueChain
PYTHONPATH=src .venv/bin/python scripts/run_multilingual_financial_ie_experiment.py \
  --output-dir /mnt/hdd8tb/valuechain/audits/financial_ie/multilingual-20260715/final-16-v03 \
  --input-list /mnt/hdd8tb/valuechain/audits/financial_ie/multilingual-20260715/inputs.txt \
  --concurrency 4
```

The Local LLM call goes directly to Endeavor's aggregate endpoint with `Qwen/Qwen3.6-35B-A3B`, thinking disabled,
temperature zero, concurrency four, and no Cloudflare route.

Primary artifacts are `records.jsonl`, `signals.jsonl`, `relations.jsonl`, `human_review.csv`,
`quality_issues.csv`, and `run_summary.json`. The manual audit adds `manual_signal_audit.csv`,
`manual_relation_audit.csv`, and `manual_audit_summary.json`.

## Next Gate

Do not write these outputs to production tables yet. The next bounded test should add real Taiwan periodic reports,
annotate at least 50 candidate passages per language, tighten signal category/modality validation, and add a
subsidiary-aware subject resolver. Production promotion should report both raw recall-oriented candidates and
the separately guarded graph-ready subset.
