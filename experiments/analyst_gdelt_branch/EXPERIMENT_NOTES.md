# Experiment Notes

## Completed Smoke Runs

SEC analyst lens:

```bash
python experiments/analyst_gdelt_branch/scripts/build_analyst_lens.py \
  --run-id industry-sec-exhibits-v3
```

Input run:

```text
industry-sec-exhibits-v3
40 companies
348 filings
801 source documents
69,121 passages
30,211 candidate passages
24,116 relation evidence rows
6,557 graph edges
```

GDELT smoke:

```bash
python experiments/analyst_gdelt_branch/scripts/fetch_gdelt_news.py \
  --tickers NVDA,AMD,CEG,DLR \
  --start 2026-05-01 \
  --end 2026-05-07 \
  --max-records 50 \
  --concurrency 2
```

Fetched 101 deduplicated article records:

```text
NVDA 43
AMD 35
CEG 20
DLR 3
```

## Early Read

The analyst lens is directionally useful. It converts graph output into tables that an
ETF analyst can scan:

- `company_scorecard.csv`: company-level dependency intensity and buckets
- `bottleneck_thesis.csv`: object-level chokepoint candidates
- `company_thesis_monitor.csv`: joined SEC scorecard + GDELT news monitor

Initial examples:

- DLR / CEG / EQIX show up as capex beneficiary watch names.
- ANET / AMD / MRVL show customer concentration watch patterns.
- CEG has a clean GDELT overlay around data center / power narrative.
- NVDA and AMD have high GDELT attention, but much of it is market-news or broad AI equity chatter.

## Failure Cases Observed

SEC-side:

- Generic class objects can dominate top object lists, e.g. "Data center or compute capacity class".
- Some EX-21 parser residue still appears, e.g. branch/legal suffix fragments such as "Pte Ltd".
- Geography objects can be real but need separate treatment from company bottlenecks.
- Object-level bottleneck scoring still mixes operating dependencies and risk-factor geography exposure.

GDELT-side:

- Headlines include many stock-market articles rather than operating news.
- Syndicated article duplication is reduced by URL dedupe but not fully eliminated.
- NVDA / AMD queries are broad and pull unrelated semiconductor or broad market headlines.
- Company + value-chain keyword queries are useful, but need a company-only control query.

## Methodology Adjustments To Try Next

1. Split score columns into:
   - operating dependency score
   - risk disclosure score
   - geography exposure score
   - class-level dependency score
   - named-counterparty score

2. Run GDELT in three query modes:
   - company only
   - company + value-chain terms
   - company + top SEC dependency objects

3. Add a domain filter / source-quality rank:
   - keep Reuters, CNBC, AP, company IR, Utility Dive, Data Center Dynamics, etc.
   - downweight generic stock sites and duplicated filings summaries

4. Add an event taxonomy:
   - earnings / guidance
   - capex / data center buildout
   - power / grid constraint
   - customer concentration
   - supplier or foundry dependency
   - export control / geopolitical risk
   - strategic partnership / contract

5. Use LLM extraction only after GDELT headline filtering:
   - first classify headlines cheaply by heuristic
   - then send top candidate titles/snippets to local LLM for event schema extraction

## Current Verdict

This is worth continuing. The promising product form is not a raw graph view; it is a
set of analyst screens:

```text
Company dependency scorecard
Bottleneck thesis table
SEC evidence inspector
GDELT event monitor
Combined thesis pressure monitor
```

The graph remains the evidence substrate. The analyst screens are the investor-facing
abstraction layer.

