# Moonbow data contracts and merge policy

This contract makes the SEC pipeline reproducible.  A later layer may add
interpretation, but it must never overwrite the text, span, or source record
that supported an earlier layer.

## Layer contract

| Layer | Artifact / table | One row means | Immutable source fields |
| --- | --- | --- | --- |
| Source | `passages.jsonl` / `passages` | one parsed SEC paragraph or table row | filing, accession, URL, section, text, parser version |
| Mention | `entity_mentions.jsonl` / `entity_mentions` | one exact named-entity or safe issuer-reference span in one passage | `passage_id`, `start_offset`, `end_offset`, `text`, extraction method |
| Alias cluster | `mention_clusters.jsonl` / `mention_clusters` | deterministic grouping of same normalized name before canonical entity selection | member mention ids are recoverable through `cluster_id` |
| Resolution record | `entity_resolution_records.jsonl` / `entity_resolution_records` | one relation-linked unresolved mention/object, its priority and candidate set | original mention, filings, evidence counts, graph impact |
| Resolution evidence | embedded in resolution record | provenance for a candidate: alias, GLEIF, identifier, parent or filing evidence | source, retrieval time, candidate identifiers |
| LLM assessment | embedded in resolution record / decision event | MATCH, NO_MATCH or UNCERTAIN for every supplied candidate | candidate-specific reason and used evidence |
| Safety validation | embedded in resolution record / decision event | uniqueness/conflict checks after LLM assessment | validation status and exact conflict reason |
| Relation evidence | `relation_evidence.jsonl` / `relation_evidence` | one issuer-centric proposed claim with an evidence passage | extracted subject/object, relation type, evidence text, model/rule version |
| Canonical entity | `canonical_entities.jsonl` / `canonical_entities` | one graph node, possibly backed by many mentions and aliases | canonical name, identifiers, resolution status |
| Canonical relationship | `canonical_relationships.jsonl` / `canonical_relationships` | one normalized, directed business fact with aggregated evidence | endpoint ids, type/family, product/service, evidence ids, source accessions |
| Decision/audit | `canonical_relationship_audit.json`, `relationship_audits`, review import | latest conclusion plus append-only audit history | reviewer/model, timestamp, reason, evidence references |

The contract deliberately separates **what appeared in a filing** from **what
we believe it identifies**.  An unresolved mention is retained; it does not
become a graph node or relationship merely because it has an organization-like
name.

## Mention and entity rules

- `mention_id` is deterministic from `passage_id + start_offset + end_offset + normalized_name`.
  It is stable across a rerun using unchanged parser output.
- Named entities use `mention_kind=named_entity`.  Only `We`, `Our`, `Us`,
  `the Company`, and `the Registrant` become `issuer_reference`, and only when
  the filing issuer is known.  Vague phrases such as “our foundry” never map to
  a company.
- `universe_alias` means an exact alias matched a known universe company;
  `name_resolved` means a legal-name pattern was found; `unresolved` means a
  plausible name awaiting resolution.  These labels state method, not truth.
- `mention_clusters` use an exact normalized key.  We do not fuzzy-merge two
  legal names in the automatic path.  A later GLEIF/LLM or human mapping may
  propose an alias link with an audit record.
- Canonical entity selection must preserve the original mention and cluster.
  Parent/subsidiary presentation collapse is a UI view, not deletion or a
  change to the legal-entity record.

## Canonical relationship merge policy

`canonicalization.canonical_merge_key` is the sole automatic merge key:

```text
(supplier_entity_id, customer_entity_id, relationship_type,
 modality, normalized_product_or_service, relationship_family)
```

Therefore the following are **never** automatically merged:

- opposite directions (`A → B` versus `B → A`);
- different top-level families, including supply chain, corporate transaction,
  and ownership/control;
- different canonical relation types or modality;
- different explicit product/service values (for example `memory` versus
  `semiconductor wafers`).

Rows with the same key merge by unioning unique `passage_id` evidence,
accessions, forms, issuer names, source relation categories, and date range.
Confidence is the mean of the source evidence scores; it is not inflated by
the count.  The relationship id is a hash of the same key, so it is stable.

## Entity-grounded extraction policy

Relation extraction is downstream of the Mention layer.  For every proposed
named company or organization object, the extractor must find a matching
`named_entity` mention in the **same passage**.  It then stores the mention's
normalized name.  A named object without this grounding is dropped with
`action=drop_unmentioned_named_object` in `mention_constraint_diagnostics.csv`.
This blocks invented counterparties while preserving anonymous classes such as
“limited number of suppliers” and geographic exposure, which are valid
evidence but cannot create a canonical company edge.

`entity_triage.csv` routes every cluster as geography, product/technology,
person/title, non-entity fragment, resolved company, or organization candidate.
`alias_review_queue.csv` contains only unresolved organization candidates. Reviewers
can map one to an existing canonical entity, create a new entity, or mark it a
non-entity.  The queue is a review aid; it never changes graph data by itself.

### Alias-resolution decision policy

```text
Candidate generation → LLM entity resolution → Safety validation → Decision engine

MATCH + PASS + no competing MATCH / identifier conflict = AUTO_ACCEPT
NO_MATCH                                                = KEEP_UNRESOLVED
UNCERTAIN or validation conflict                        = REVIEW
```

Safety Validation is not rule-based entity resolution and never filters a
relation-linked mention out of the pipeline. It checks only decision safety:
candidate uniqueness, confirmed aliases, parent/subsidiary consistency,
identifier/jurisdiction conflicts, multiple MATCH results and obvious
contradictions. The local LLM is the primary resolver among the supplied
candidate set; it must return a per-candidate assessment, confidence, concise
reason and `used_evidence` list. Candidate sources may include the current and
historical graph, aliases, SEC identifiers, subsidiary records, GLEIF, and
future registries, provided their provenance is recorded.

`auto_accept` is an accepted **alias mapping decision**, not an immediate
write to `canonical_entities`.  The decision artifact keeps the GLEIF record,
hard-check outcome, LLM reason and policy version. A later canonical refresh
may consume accepted mappings; unresolved and human-review records cannot.

Cross-filing verification is a decision signal, not a separate graph fact:
two or more distinct SEC accessions supporting the same merge key, each at
confidence at least `0.75`, set `decision=accept`,
`review_status=accepted`, and `decision_source=cross_filing_rule`.  A clear
single-source statement remains a candidate until an LLM or human audit accepts
it; it is not discarded.

## Decision and rerun policy

- Raw evidence and mentions are append/rebuild artifacts; they are never
  overwritten by review text.
- A human or LLM decision is stored as an audit event.  The displayed decision
  is the latest valid conclusion; earlier conclusions remain in audit history.
- On a rerun, prior reviewed decisions can transfer only when the canonical
  fingerprint still matches (including endpoints, family/type, modality and
  product).  This prevents a newly extracted claim from inheriting a decision
  for a different fact.
- Automatic cross-filing acceptance cannot overwrite an explicit human reject.
  The human decision is retained and the conflicting verification is surfaced
  for review.

## Team-facing database policy

Postgres is the shared serving copy.  Pipeline writes are transactional per
run: first the run manifest, then raw source/passage/mention data and the
canonical graph.  The `entity_mentions` and `mention_clusters` tables have no
foreign key to canonical entities on purpose: unresolved source mentions must
be storable.  Review/audit sync updates decisions without replacing raw facts.

Consumers should use `canonical_relationships` for the map and join through
`evidence_ids → relation_evidence → passages` to display source text.  They
should not infer a relationship directly from a mention cluster.
