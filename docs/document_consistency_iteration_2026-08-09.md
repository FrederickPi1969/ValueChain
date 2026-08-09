# Long-document consistency iteration, 2026-08-09

The production pipeline now reconciles entities before relation extraction and reconciles relation evidence
after denoising. This layer is deterministic and filing-scoped; it does not ask an LLM to silently merge names.

Entity consistency uses explicit canonical names plus locally declared aliases such as
`GLOBALFOUNDRIES Inc (GF)`. A Union-Find groups exact canonical keys, and a pure-Python Aho-Corasick automaton
rescans every passage in the same `accession_number + source_document`. Only universe aliases, legal-suffix
organizations, table-row legal entities, and acronyms explicitly attached to those trusted mentions enter the
automaton. Existing spans cannot be overwritten. Each recovered mention retains passage-local offsets and gets a
stable mention, cluster, and canonical entity ID.

Relation consistency removes only identical assertions repeated across passages, using accession, canonical
endpoints, raw relation type, modality, and normalized evidence text. Different evidence remains separate. If
the same canonical relation and entity pair appears in both orientations in one filing, neither side is silently
selected: both receive `document_direction_conflict`, and the event is written to merge diagnostics.

## Tests

- Synthetic tests cover alias declaration/rescan, longest-match behavior, word boundaries, overlap deduplication,
  stable clustering, and explicit direction conflicts.
- On 3,000 real AMD, Equinix, and Meta passages, the corrected scanner added 128 mentions in 0.79 seconds. Manual
  spot checks included `GF`, `UMC`, `SPIL`, `KYEC`, and `THATIC`. An earlier permissive candidate policy added
  6,323 noisy mentions and was rejected before merge.
- On 2,710 existing relations for the same issuers, cross-passage reconciliation removed 9 exact duplicates and
  surfaced 3 direction-conflict groups without discarding their evidence.
