# US earnings-call history campaign

Date: 2026-08-11

## Why collect it

Earnings calls are materially useful to ValueChain because management remarks
and analyst Q&A contain forward demand, capacity, capex, customer, supplier,
inventory, pricing, and competitive signals that are absent or delayed in SEC
filings. The highest marginal value is normally in the latest 8–12 quarters;
2020–2022 remains useful for cycle and shock comparisons but should earn its
cost through a pilot before full expansion.

## Audited scope

The two 1,000-company cohorts contain 1,593 issuers whose downloaded 2025 10-K
identifies only US state/DC/United States incorporation facts:

- top cohort: 882
- next cohort: 711
- US territories are excluded
- foreign or missing incorporation facts are excluded

As of 2026-08-11, the complete cross-company target-label range is 2020 Q1
through 2026 Q2: 26 periods and 41,418 company-quarter jobs. Q3/Q4 2026 are
not yet uniformly available across the universe and will be later increments.

The search target is the company's reported result label, not the calendar
bucket containing the call date. For example, `MSFT FY26 Q1` remains the 2026
Q1 target even though the call occurred in October 2025. The initializer's
legacy `calendar_year/calendar_target` columns carry that target label; runtime
metadata makes the semantics explicit as `target_year`, `target_quarter`, and
`target_period_label`, alongside `fiscal_year`, `fiscal_quarter`, `period_end`,
`call_date`, and `reported_period_label`. Call dates are descriptive and never
cause a correct fiscal/result label to be rejected.

## Rollout

The initialized pilot is 100 deterministic, cohort/sector-stratified companies
crossed with Q2 of 2020–2026: 700 company-quarter jobs. The database is:

`data/earnings_calls/history/us_2020_2026_pilot.sqlite3`

Jobs are recent-first, lease-fenced, retryable, resumable, and capped at four
search calls. The default search policy is four DuckDuckGo queries, following
the paired search study; Google is reserved for a later explicit recovery
campaign.

### Live 2026 Q2 pilot status

The first 100 recent-period jobs have completed their bounded Pathfinder run:

- 82 strict, compressed transcripts accepted and 18 exhausted;
- 194 DuckDuckGo search submissions, 1,940 saved candidate links, and zero
  search-service failures;
- 1.94 queries per company on average, with a hard maximum of four;
- 24 risk-stratified accepted transcripts manually reviewed: 24/24 were the
  correct company, exact period, and complete call (observed precision 100%;
  Wilson 95% lower bound approximately 86.2%);
- every accepted artifact passed candidate identity, exact manifest, SHA-256,
  transcript length, and ZSTD integrity checks.

The initial result was 75/100. Audit found seven policy false negatives without
issuing another search: VRSK, CPAY, USFD, TAP, CTVA, and ARE had explicit
company/period/earnings-call YouTube titles that Qwen rejected only because the
uploads were third-party, while CLB had a complete management sign-off ending
in `Goodbye for now`. The link prompt and deterministic rules now accept an
explicitly matching YouTube call regardless of officiality but still reject
summary/highlight/analysis videos. Expanded closing rules and a reverse-
duplicate recovery fix then brought the same 100 jobs to 82/100.

The smoke also fixed candidate retries consuming a company's infrastructure
retry budget, named operator endings, overlapping caption timestamps, and a
Qwen false negative on MSTR's complete 2-hour-35-minute YouTube transcript.
All recoveries create new immutable versions; old evidence is never mutated.

Validation prompt `earnings-full-call-v2-grounded-dates` was frozen after three
small-sample rounds. The final eight-source study produced valid JSON for 8/8,
kept all eight full-call decisions correct, grounded three call dates and one
period end in visible text, and left unsupported dates null. Exact target
fiscal labels override conflicting calendar wording only after
`period_match=true`. Prompts and outputs are retained compressed at
`data/earnings_calls/studies/validation_prompt_metadata_20260811/results.jsonl.zst`.

This completes the recent 100-company slice.  The remaining 600 Q2 pilot jobs
for 2020--2025 were released on 2026-08-11 with eight workers, DuckDuckGo-only
discovery, the same four-query cap, Zstandard bundles, and Mac-mini-only browser
fallback.  The live source of truth for their changing status is the campaign
SQLite database rather than a number copied into this document.  This is still
a pilot, not the 41,418-job universe. Historical Cosmos publishing remains
deliberately disabled until the complete 700-job coverage and downstream-signal
gates below pass.

Promote from 700 jobs to the full 41,418 only if:

- clean transcript coverage is at least 35% for recent periods and 20% for
  2020–2022;
- artifact/manifest/hash audit success is at least 99%;
- no job exceeds four actual search submissions;
- the extracted relation signal improves downstream localization or analysis
  by at least 20% versus the time/cost baseline.

After the pilot, expand recent-first: 2026 Q2, repair 2026 Q1 and 2025 Q4,
then all of 2025 and 2024, followed by 2023 backward only while marginal yield
remains acceptable.

## Expected cost

The complete 41,418-job universe is operationally feasible but should not be
launched blindly. Based on the current compressed corpus, successful bundles
would occupy roughly 1.5 GiB if every job succeeded; a realistic clean corpus
is approximately 0.4–0.6 GiB. Reserve 5–10 GiB for immutable versions,
rejected evidence, retries, and staging.

The expensive part is discovery and validation rather than storage. Depending
on age-related recall, expect roughly 10,700–17,000 clean transcripts and a
multi-week acquisition run. A quarterly incremental worker is preferable to a
periodic full rescan: enqueue the newly closed quarter, retry only transient
failures, and run a separate low-priority recovery campaign for exhausted jobs.
