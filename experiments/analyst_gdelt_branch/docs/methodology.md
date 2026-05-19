# Methodology Sketch

## Why Raw Knowledge Graph Is Not Enough

A knowledge graph is good for provenance and relationship traversal. It is weaker as an
investment-facing surface because portfolio managers usually ask ranked questions:

- Which companies have increasing exposure to a constraint?
- Which dependency is a sector-wide bottleneck?
- Which disclosures are current operating facts vs. risk-factor boilerplate?
- Which edges are investable thesis signals rather than incidental legal text?
- Which names are capex beneficiaries, and which names are dependency takers?

This branch treats the graph as an input, then builds analyst lenses on top.

## Proposed Analyst Lenses

### 1. Chokepoint Exposure

Rank dependency objects by:

- number of dependent companies
- number of evidence rows
- number of forms / accessions
- share of current-fact evidence
- relation type mix

This highlights bottleneck candidates such as foundries, cloud providers, power supply,
data centers, customers, geographies, or key suppliers.

### 2. Company Dependency Intensity

For each company:

- total evidence count
- edge count
- unique dependency object count
- current-fact evidence count
- risk-hypothetical evidence count
- forward-looking evidence count
- supplier / customer / cloud / data center / power / foundry relation counts

This yields a scorecard that is easier to scan than a graph.

The current implementation expands this into percentile factors:

- `dependency_risk_pct`
- `operating_dependency_pct`
- `chokepoint_exposure_pct`
- `customer_concentration_pct`
- `capex_beneficiary_pct`
- `fragility_pct`
- `evidence_quality_pct`
- `investment_relevance_pct`

These are screening factors, not final alpha factors. They are designed to rank where an
ETF analyst should spend review time.

### 3. Modality Mix

Separate:

- current_fact: disclosed operating dependency
- risk_hypothetical: conditional risk factor
- forward_looking: planned or expected relationship
- historical_fact: past concentration or dependency
- strategic: partnership / alliance / co-investment

For financial users, current facts and repeated disclosures should carry more weight than
generic "may be affected" language.

### 4. Capex Beneficiary vs. Dependency Taker

A company can be:

- a dependency taker: relies on scarce foundry, cloud, power, customer, or supplier nodes
- a bottleneck/enabler: is itself repeatedly named as a dependency object
- a capex beneficiary: sells into data center, grid, cooling, server, optical, or power buildout

This is closer to an equity research framing than graph traversal.

### 5. News Overlay

GDELT is not a source of SEC-grade dependency evidence. It is useful for:

- event monitoring
- narrative momentum
- topic mix by company
- whether disclosed dependencies are becoming news-active
- article count spikes around earnings, regulation, power constraints, export controls, or capex

The output should be joined analytically, not written into the dependency evidence tables.

## Experimental Score Ideas

These are intentionally simple first-pass scores:

```text
dependency_intensity = log1p(evidence_count) + 0.5 * log1p(edge_count)
fragility_ratio = (risk_hypothetical + forward_looking) / (current_fact + 1)
chokepoint_exposure = sum(dependent_company_count for dependency objects)
concentration_watch = customer_dependency + concentration_risk
power_data_center_exposure = power + data_center + cloud_or_hosting
```

These are not final investment factors. They are screening features for analyst review.

## GDELT Query Design

Use three query modes rather than one broad search:

```text
company:
  "NVIDIA"

value_chain:
  "NVIDIA" (AI OR "artificial intelligence" OR GPU OR semiconductor OR chip OR "data center" OR datacenter OR cloud OR power OR grid OR foundry OR HBM)

sec_object:
  "NVIDIA" "Taiwan Semiconductor Manufacturing Company Limited"
```

The query modes have different jobs:

- company-only gives a control sample and catches broad company news
- value-chain query increases recall for AI infrastructure narratives
- SEC-object query tests whether disclosed counterparties are becoming news-active

The large validation run shows that `sec_object` has high average relevance but lower
coverage and more attribution risk. It should be used as a drilldown query, not as the
only news retrieval path.

## News NLP Layers

The news overlay now has two layers:

1. Heuristic annotation:
   - event theme keyword hits
   - source tier
   - query-mode provenance
   - headline quality flags
   - event relevance score

2. Local LLM event framing:
   - event type
   - materiality score 0-3
   - value-chain relevance score 0-3
   - direction
   - dependency object
   - short event summary

The LLM layer is intentionally applied after filtering. It is not allowed to create graph
edges and it should not override SEC evidence. Its job is to reduce headline noise and
separate operating events from stock-market commentary.

## Combined Monitor Logic

The combined monitor joins SEC factors, heuristic news scores, and sampled LLM event
frames:

```text
monitor_priority =
  SEC structural score
  + news event score
  + SEC/news theme alignment
  + source/coverage quality
  + sampled LLM materiality
```

Important caps:

- dominant `market_reaction` news is capped and labeled `sec_thesis_market_noise`
- `other` news with weak average event relevance is capped
- missing GDELT coverage does not erase SEC-only thesis rows
- GDELT is never treated as dependency evidence

## Failure Modes

- GDELT article titles can be duplicated across syndication sites.
- Company names can be ambiguous.
- News volume favors mega-cap names and media-active stocks.
- Article count is not sentiment.
- Headlines can be stock-market commentary rather than operating events.
- GDELT context cannot replace SEC provenance.
- SEC-object search can retrieve a sector article mentioning the object but not actually
  supporting the ticker-level thesis.
- LLM headline classification can infer too much from weak titles, so the prompt must
  require "use only supplied fields" and return low materiality for market-only headlines.
- External APIs can rate-limit large parallel runs; production needs checkpointed job
  queues and resumable retries.

## What Would Count As Useful

The experiment is promising if it produces:

- a company scorecard ETF managers can scan in under one minute
- a bottleneck thesis table with clear relation-type and modality mix
- a news overlay that surfaces relevant recent events without overwhelming noise
- a repeatable method that can be rerun from local artifacts without touching the database
