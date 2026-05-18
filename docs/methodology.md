# Prototype Methodology

## Scope

The first prototype targets an AI infrastructure universe rather than a
single-issuer extraction demo. The default universe is in
`data/universe/ai_infra_universe.csv` and includes AI accelerators, foundries,
semiconductor equipment, networking, servers, data centers, power, cloud, and
AI software names. Runs can be scoped by role, priority, ticker subset, filing
type, and filing date window.

## Source Posture

SEC official data remains the source of truth:

- submissions JSON discovers which filings exist;
- SEC archive documents provide the evidence-bearing text;
- Yahoo Finance is optional enrichment for portfolio context;
- vendor APIs are not required for the prototype.

The crawler uses a single process and a configurable global rate limiter. Set a
meaningful `VALUECHAIN_SEC_USER_AGENT` before running larger jobs.
Use `valuechain plan` before broad runs to estimate upper-bound SEC requests and
confirm role coverage.

## Extraction Contract

The prototype separates:

- parsed passages;
- entity mentions and normalized companies;
- relation evidence;
- aggregated graph edges.

Relation evidence keeps the original passage text and source provenance so a
portfolio manager or annotator can inspect why an edge exists. Risk-factor
language is labeled separately from current operating dependencies.

## Denoising and Merge

The prototype keeps two evidence layers:

- `relation_evidence_raw.jsonl` is the extractor output before graph filtering.
- `relation_evidence.jsonl` is graph-ready evidence after schema-aware gating.

The gate applies relation-specific policy from `config/ontology.yaml`: named
counterparties are required for strategic partners and co-investments; generic
supplier/cloud placeholders are dropped unless the passage contains explicit
concentration or third-party hosting reliance; class objects are allowed only
when the passage supports a constrained resource or exposure. The merge layer
canonicalizes common aliases such as AWS/Amazon.com, Azure/Microsoft,
GCP/Alphabet, and TSMC/Taiwan Semiconductor. `merge_diagnostics.csv` records
every keep/drop decision so thresholds can be audited.

An optional embedding merge step can cluster surviving object labels with the
local `qwen3-embed-0.6b` embedding model through Endeavor's aggregate endpoint.
It is intended for alias discovery after deterministic filtering, not as a
replacement for provenance or manual validation.

## Presentation Choice

The first presentation surface is a portfolio-oriented evidence dashboard, not a
force-directed graph. For ETF managers, tabular triage is usually faster:

- company exposure table;
- bottleneck candidates;
- aggregated typed edges;
- evidence inspector with filing links.

Graph visualization can be added after the evidence table has enough validated
signal to justify network exploration.
