# Prototype Methodology

## Scope

The first prototype targets a small AI infrastructure universe rather than a
complete global industry map. The default universe is in
`data/universe/ai_infra_universe.csv` and includes AI accelerators, foundries,
semiconductor equipment, networking, servers, data centers, power, cloud, and
AI software names.

## Source Posture

SEC official data remains the source of truth:

- submissions JSON discovers which filings exist;
- SEC archive documents provide the evidence-bearing text;
- Yahoo Finance is optional enrichment for portfolio context;
- vendor APIs are not required for the prototype.

The crawler uses a single process and a configurable global rate limiter. Set a
meaningful `VALUECHAIN_SEC_USER_AGENT` before running larger jobs.

## Extraction Contract

The prototype separates:

- parsed passages;
- entity mentions and normalized companies;
- relation evidence;
- aggregated graph edges.

Relation evidence keeps the original passage text and source provenance so a
portfolio manager or annotator can inspect why an edge exists. Risk-factor
language is labeled separately from current operating dependencies.

## Presentation Choice

The first presentation surface is a portfolio-oriented evidence dashboard, not a
force-directed graph. For ETF managers, tabular triage is usually faster:

- company exposure table;
- bottleneck candidates;
- aggregated typed edges;
- evidence inspector with filing links.

Graph visualization can be added after the evidence table has enough validated
signal to justify network exploration.

