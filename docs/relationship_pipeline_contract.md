# Relationship pipeline contract

This contract prevents independently evolving stages from changing the same
relationship fact or silently overwriting a prior conclusion.

## Saved-corpus re-extraction workflow

Extraction improvements must be tested against the immutable saved SEC corpus
before they replace audited artifacts:

```bash
valuechain reextract-relationships --run-id <run-id> --extractor rules --preview-id <name>
```

The command writes a self-contained preview below
`data/processed/runs/<run-id>/reextractions/<name>/`. It recomputes mentions,
relation assertions, product/service attributes, canonical candidates, and a
delta summary, but never overwrites the run's current evidence, canonical
relationships, audits, or Postgres projection. Promotion must reconcile changed
entity identities and preserve only decisions whose relationship identity stays
the same.

## One-way data flow

`Passage → RelationEvidence → normalized evidence → canonical candidate → audit / human decision → published graph`

Every stage appends provenance or produces a new artifact.  It must not edit an
earlier stage's source text, named spans, or raw extraction output.

## Ownership boundaries

| Owner | May do | Must not do |
| --- | --- | --- |
| `relation_rules.py`, `relation_llm.py` | Extract recall-first `RelationEvidence`, including exact evidence text and product/service when stated. | Confirm, reject, or canonicalize a relationship. |
| `edge_quality.py` | Normalize objects; discard parser fragments; retain named ambiguity with `risk_flags`. | Reverse an edge, assign `accepted`, or use a keyword as a truth decision. |
| `canonicalization.py` | Build typed `source → target` candidate edges; aggregate provenance; run conservative cross-filing verification. | Use world knowledge; overwrite an audit/human conclusion; rewrite direction from a surface phrase. |
| `evidence_audit.py` | Make the current LLM conclusion from supplied SEC evidence and retain audit history. | Use outside knowledge or mutate raw evidence. |
| `human_review.py` | Apply an explicit CSV/UI decision. | Let a blank row undo an automatic decision; inherit a decision across reversed endpoints. |
| `postgres.py`, `dashboard.py`, frontend | Persist and render the current canonical layer. | Recompute truth, status, direction, or relation family. |

## Canonical relationship contract

The canonical record has one authoritative endpoint pair:

`source_entity_id`, `source_entity_name`, `source_role` → `target_entity_id`, `target_entity_name`, `target_role`.

Legacy `supplier_*` / `customer_*` fields are display compatibility only. They
must not be used to infer the direction of non-supply relationships.

`risk_flags` are evidence-quality warnings, never a verdict. Current flags:

- `competitor_or_market_context`
- `direction_anomaly`
- `cross_sentence_attachment_risk`
- `product_or_service_not_extracted`

## Decision precedence and merge policy

1. A new canonical candidate starts `unreviewed` / `pending_review`.
2. Cross-filing verification may set `accepted` only when there are at least two
   filings, strong evidence, and no blocking context/direction flags.
3. A current valid LLM audit sets the visible conclusion for the *same
   relationship_id*.
4. An explicit human decision may override it for that exact id.
5. Across a changed relationship id, only an explicit human decision may be
   inherited, matching source, target, family, type, and modality. LLM and
   cross-filing results never inherit heuristically.
6. A flagged candidate must be re-audited; an old cross-filing audit cannot
   override a current blocking flag.

## Required safeguards for future changes

- Add a risk flag or diagnostic before adding a direct lexical correction.
- Any new automated status change needs a regression test for both the positive
  case and a direction/context counterexample.
- Do not add issuer names, customer names, or a one-company sentence pattern to
  truth logic.
- Any new output field must be added consistently to: model, canonical JSON,
  Postgres sync/API, dashboard export, and frontend renderer.
- Use `refresh-canonical` then audit pending candidates; do not manually edit
  generated JSONL as a state-repair mechanism.

## Artifact authority

- `relation_evidence.jsonl`: immutable extraction evidence for a run.
- `canonical_relationships.jsonl`: current graph candidate and decision view.
- `canonical_relationship_audit.json`: append-preserved LLM audit ledger.
- `canonical_relationships_reviewed.jsonl`: export of explicit review state;
  not an independent source of LLM truth.
- Postgres: shared durable projection and audit recovery source.
