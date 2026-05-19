# Large Run Findings: SEC Dependency Factors + GDELT Event Overlay

Run date: 2026-05-18

This memo summarizes the exploratory branch only. Outputs are local files under
`experiments/analyst_gdelt_branch/outputs/` and are not written to Postgres.

## Executive Read

The useful product shape is an analyst monitor, not a raw graph. The strongest screen is:

```text
SEC-derived structural dependency score
  + GDELT value-chain event attention
  + Local LLM event/materiality classification
  + explicit market-noise caps
```

The monitor surfaces two different but investable categories:

- infrastructure beneficiaries with SEC-backed operating exposure and current news momentum
- semiconductor / platform names with customer concentration or supply-chain chokepoint risk

The big warning is that news volume is noisy. The LLM pass found that `market_price_action`
was the largest single event class in the sampled headlines. That confirms the need to
separate "stock is moving" from "operating dependency changed."

## Scale

SEC base artifact:

```text
run_id: industry-sec-exhibits-v3
companies: 40
filings: 348
source documents: 801
passages: 69,121
candidate passages: 30,211
relation evidence rows: 24,116
typed graph edges: 6,557
```

News overlay:

```text
companies queried: 20
initial query specs: 80
slow NEE retry specs: 4
merged deduplicated articles: 1,285
companies covered: 20
LLM-classified high-score articles: 140
LLM parse errors: 0
```

## Top Combined Monitors

| Ticker | Tier | Read |
| --- | --- | --- |
| AMD | high_priority | SEC customer concentration watch plus AI demand/capex news. |
| CEG | high_priority | SEC capex beneficiary watch plus data center power news. |
| EQIX | high_priority | SEC data center exposure plus AI infrastructure news. |
| NVDA | high_priority | SEC customer concentration watch plus AI demand/capex news. |
| DLR | high_priority | SEC data center exposure plus AI infrastructure news. |
| ANET | sec_thesis_market_noise | SEC thesis exists, but recent news is mostly market reaction. |
| CSCO | sec_thesis_market_noise | SEC operating dependency signal exists, news is mostly market reaction. |
| DELL | active_watch | SEC operating/evidence-quality signal plus AI demand/capex news. |
| AMAT | sec_thesis_market_noise | Strong structural relevance, but current news overlay is market-heavy. |
| SMCI | active_watch | Chokepoint exposure plus AI demand/capex news. |

NEE is no longer empty after a slow retry:

```text
NEE: 78 articles, dominant heuristic theme data_center_power,
combined tier background_watch.
```

This is directionally right. NEE has strong news relevance around power/data center themes,
but this SEC artifact does not yet give it the same structural dependency score as CEG,
DLR, or EQIX. The monitor should keep it visible without letting news alone dominate.

## Query-Mode Lessons

| Query Mode | Articles | Avg Event Relevance | Interpretation |
| --- | ---: | ---: | --- |
| value_chain | 813 | 2.802 | Best recall for AI infrastructure narrative. |
| company | 876 | 2.485 | Useful control, but noisy for mega-caps. |
| sec_object | 76 | 4.571 | Highest relevance, lowest coverage, higher attribution risk. |

`sec_object` search is worth keeping, but it should be a drilldown accelerator rather than
the main news source.

## LLM Event-Frame Lessons

Sampled high-score headlines, classified by Qwen/Qwen3.5-4B through the Endeavor aggregate
Local LLM endpoint:

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

This proves why a two-stage NLP approach is needed:

- keywords are good for recall
- source tiering and quality flags reduce obvious junk
- LLM event frames help identify stock-market chatter and headline-only false positives
- SEC evidence still anchors the operating thesis

## Reliability Assessment

Promising:

- SEC-derived factors are stable and explainable because every row traces back to filing evidence.
- GDELT expands recency and narrative coverage without polluting the dependency graph.
- LLM classification was fast and produced parseable JSON on 140/140 records.
- The monitor correctly separates NEE news strength from SEC structural ranking.

Current weaknesses:

- SEC object extraction still includes parser residue and legal suffix fragments.
- GDELT headline-only classification lacks article body context.
- SEC-object query can attach sector articles to the wrong ticker.
- Market-wrap / analyst-rating headlines can dominate company coverage.
- Rate limits require checkpointed retrieval, retries, and query-level status metadata.

## Next Optimization Plan

1. Add retrieval accounting:
   per-query status, retry count, rate-limit flag, and no-coverage vs. failed-retrieval distinction.

2. Add ticker-attribution NLP:
   classify whether the article subject is the queried issuer, a counterparty, a peer, or a macro/sector story.

3. Improve SEC entity cleanup:
   demote legal suffix fragments, geography-only objects, and generic class objects in `top_named_counterparties`.

4. Add event-body enrichment:
   fetch legally accessible snippets/full text for top articles and rerun event framing on richer context.

5. Add factor backtesting hooks:
   join monitor scores to next-period returns, sector-relative returns, revisions, and volatility to see whether
   the screen is useful beyond narrative quality.

6. Add portfolio-facing outputs:
   top 20 monitor table, sector exposure table, bottleneck objects, and evidence drilldown links for each thesis row.
