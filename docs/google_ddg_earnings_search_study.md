# Google vs DuckDuckGo earnings-call search study

Date: 2026-08-11

## Question

Can the ValueChain earnings-call pipeline use DuckDuckGo as its primary search
engine without materially reducing the number of complete transcripts it can
retrieve?

## Design

- Target: Q1 2026 earnings calls.
- Sample: 60 fixed companies, ten from each of six pre-existing strata:
  `top/next` crossed with `validated/exhausted/no_accepted`.
- Population weights: 2,000 companies (`52/598/350` in the top cohort and
  `372/438/190` in the next cohort).
- Both engines received the same ordered query policy, with a maximum of four
  queries per company:
  1. `<ticker> <company> 2026 Q1 earnings conference call`
  2. the same query plus `YouTube`
  3. `earnings call transcript`
  4. `quarterly results conference call`
- A result counted as successful only after the URL was downloaded, normalized,
  checked by Qwen for the company and exact period, and passed the deterministic
  full-call gate. A plausible link, results release, SEC filing, presentation,
  or partial transcript did not count.
- Every query, link, judgement, download trial, error, and compressed artifact
  is retained in `data/earnings_calls/studies/google_ddg_q1_2026.sqlite3` and the
  adjacent artifact directory.

## Results

| Metric | DuckDuckGo | Google |
|---|---:|---:|
| Complete transcripts, raw | 47/60 (78.3%) | 14/60 (23.3%) |
| Population-weighted success | 81.78% | 18.23% |
| Success after query 1 | 36/60 (60.0%) | 3/60 (5.0%) |
| Success after query 2 | 40/60 (66.7%) | 14/60 (23.3%) |
| Success after query 3 | 42/60 (70.0%) | 14/60 (23.3%) |
| Success after query 4 | 47/60 (78.3%) | 14/60 (23.3%) |
| Search requests issued | 122 | 209 |
| Mean search latency | 8.91 s | 1.61 s |
| Search-service errors | 0 | 0 |

Paired outcomes were: both engines succeeded for 13 companies, DuckDuckGo only
for 34, Google only for one, and neither for 12. The exact two-sided McNemar
test gives `p = 2.10e-9`. The population-weighted DuckDuckGo-minus-Google
difference was 63.55 percentage points; its stratified bootstrap 95% interval
was 50.48 to 75.07 points.

If all four DuckDuckGo attempts run first and Google is used only for DuckDuckGo
misses, the observed model reduces Google requests by 76.1% raw and 80.1% after
population weighting. The union adds only one success over four-query
DuckDuckGo alone in this sample.

## Decision

Use DuckDuckGo for all four default production queries. It is slower per
request, but early stopping kept the average to 2.03 queries per company and it
produced much higher strict end-to-end recall. Keep Google as an explicit
recovery campaign rather than consuming one of the normal four-query slots.

The result is specific to this service configuration, query policy, Q1 2026,
and the current downloadable web corpus. Re-run a smaller sentinel study each
month so a backend/index or extraction regression is detected before a large
campaign.
