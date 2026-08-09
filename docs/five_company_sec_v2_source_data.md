# Five-company SEC v2 source-data package

This package is the processed output of the SEC-first relationship pipeline for
NVIDIA, AMD, TSMC, Micron, and Samsung. It is intended for review, not as a
replacement for the SEC archive.

## Read in this order

1. `run_summary.json` — run configuration and record counts.
2. `source_document_manifest.csv` — every filing/exhibit used, including the
   accession number, SEC URL, and SHA-256 hash.
3. `relation_evidence.jsonl` — retained relationship assertions. Each row has
   its own `evidence_id`, exact evidence text, subject/object, relation type,
   passage ID, and SEC document URL.
4. `canonical_relationships_reviewed.jsonl` — canonical edges, current
   decision, product/service attribute, risk flags, and supporting
   `evidence_ids`.
5. `canonical_relationship_audit.json` — evidence-only audit decisions and
   their decision history.
6. `relationship_lineage_events.jsonl` — canonicalization and audit events.

`passage_id` identifies a source span; it can legitimately contain multiple
assertions. `evidence_id` identifies one subject–relation–object assertion and
is the field used by canonical relationship pointers.

## Integrity snapshot after migration

- 1,788 raw assertions and 618 retained assertions, all with an `evidence_id`.
- 95 canonical relationships; every referenced evidence ID resolves to a
  retained assertion.
- 61 accepted and 34 rejected current decisions.
- 14 canonical relationships currently have an explicitly extracted
  `product_or_service` attribute.

The remaining blank product/service values are unknown, not inferred. They can
be enriched by rerunning the new extractor against the archived passages
without changing the canonical relationship ID or prior audit history.
