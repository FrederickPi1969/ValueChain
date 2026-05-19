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

## Completed Large Exploratory Run

SEC analyst lens input stayed on the existing `industry-sec-exhibits-v3` artifact:

```text
40 companies
348 filings
801 source documents
69,121 passages
30,211 candidate passages
24,116 relation evidence rows
6,557 graph edges
```

GDELT overlay:

```text
window: 2026-04-01 to 2026-05-18
companies queried: 20
query specs: 80 initial + 4 slow NEE retry
merged deduplicated articles: 1,285
companies covered: 20
```

The first 20-company run hit GDELT `429 Too Many Requests` on some later queries. NEE
was empty in that initial batch, then a slower single-company retry returned 78 records.
This means empty news coverage must be distinguished from retrieval failure.

Heuristic GDELT top rows:

```text
NEE   78 articles  event_score 327.6  dominant data_center_power
AMD   71 articles  event_score 233.2  dominant ai_demand_capex
META  95 articles  event_score 223.3  dominant other
DELL  78 articles  event_score 211.3  dominant ai_demand_capex
NVDA  67 articles  event_score 206.7  dominant ai_demand_capex
ASML  64 articles  event_score 204.9  dominant semiconductor_supply
EQIX  58 articles  event_score 191.3  dominant ai_demand_capex
CEG   57 articles  event_score 177.4  dominant data_center_power
```

Query-mode diagnostics:

```text
value_chain 813 articles  avg_event_relevance 2.802
company     876 articles  avg_event_relevance 2.485
sec_object   76 articles  avg_event_relevance 4.571
```

The `sec_object` mode is high precision when it works, but it has lower coverage and
requires attribution checks.

Local LLM event framing:

```text
model: Qwen/Qwen3.5-4B via Endeavor aggregate proxy
classified articles: 140
companies covered: 20
LLM parse errors: 0
```

Event type mix:

```text
market_price_action     43
demand_capex            27
semiconductor_supply    18
other                   17
partnership_contract    12
earnings_guidance        9
regulation_geopolitics   8
datacenter_power         3
low_signal_noise         3
```

Combined SEC + GDELT + LLM monitor:

```text
high_priority              5
active_watch               2
background_watch           7
sec_thesis_market_noise    3
low_signal                 3
sec_only_no_recent_news   20
```

Top monitor rows:

```text
AMD   90.556 high_priority  AI demand/capex overlay + SEC customer concentration watch
CEG   87.383 high_priority  data center power overlay + SEC capex beneficiary watch
EQIX  85.013 high_priority  AI demand/capex overlay + SEC data center exposure
NVDA  84.374 high_priority  AI demand/capex overlay + SEC customer concentration watch
DLR   84.302 high_priority  AI demand/capex overlay + SEC data center exposure
ANET  76.000 sec_thesis_market_noise
CSCO  73.797 sec_thesis_market_noise
DELL  71.556 active_watch
AMAT  70.526 sec_thesis_market_noise
SMCI  62.865 active_watch
```

NEE after retry:

```text
NEE 48.622 background_watch
dominant_news_theme: data_center_power
articles: 78
LLM event mix: datacenter_power / demand_capex / partnership_contract plus market noise
```

The current model does not elevate NEE to high priority because SEC structural score is
lower than the data-center REIT / CEG group in this artifact, even though news relevance
is high. This is a useful sanity check: news alone should not dominate SEC-derived thesis
ranking.

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
- SEC-object query can retrieve sector-wide articles where the queried ticker is not the
  true subject. Example: AMD + Broadcom can still surface NVIDIA/industry-comparison
  articles.
- GDELT can return 429 on multi-company runs. Empty company coverage needs retry metadata.
- Heuristic `ai_demand_capex` can over-score index / market-wrap headlines. The LLM layer
  correctly marked some of these as `low_signal_noise` or `market_price_action`.

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

6. Add retrieval accounting:
   - per-query status code
   - rate-limit count
   - retry count
   - retrieval failure vs. true no-coverage flag

7. Add ticker-attribution de-noising:
   - check whether headline contains company query name or ticker
   - demote articles whose subject company is another issuer
   - use LLM to classify `subject_company_match`

8. Add GDELT article snippets or full text where legally/practically available:
   - headline-only classification works for triage
   - investment-grade event framing needs more context than title/domain

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
