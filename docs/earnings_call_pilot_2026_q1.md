# Q1 2026 earnings-call recall pilot

Run command:

```bash
PYTHONPATH=src python3 scripts/run_earnings_call_pilot.py
```

Result: **7/7 recall**. Every accepted result was judged by local
`Qwen/Qwen3.6-35B-A3B`; the deliberately added Form 10-Q candidate for each
company was rejected. The replayable result record, including Qwen rationales,
is at `data/earnings_calls/pilot_2026_q1/pilot_summary.json`.

| Company | Retrieved call source | Local text artifact |
|---|---|---|
| Tesla | [Q1 2026 transcript](https://earnings.video/earnings/tesla/q1-2026/transcript) | `tesla/transcript.txt` |
| Microsoft | [FY26 Q1 official conference call](https://www.microsoft.com/en-us/investor/events/fy-2026/earnings-fy-2026-q1) | `microsoft/transcript.txt` |
| Google / Alphabet | [Q1 2026 transcript](https://www.fool.com/earnings/call-transcripts/2026/04/29/alphabet-googl-q1-2026-earnings-call-transcript/) | `google/transcript.txt` |
| Meta | [Q1 2026 official transcript PDF](https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/META-Q1-2026-Earnings-Call-Transcript.pdf) | `meta/transcript.txt` |
| Walmart | [Q1 FY26 official transcript PDF](https://corporate.walmart.com/content/dam/corporate/documents/newsroom/2025/05/15/walmart-releases-q1-fy26-earnings/q1-fy26-earnings-call-transcript.pdf) | `walmart/transcript.txt` |
| Nebius | [Q1 2026 transcript](https://www.benzinga.com/news/26/07/60237384/nebius-group-reports-q1-2026-results-full-earnings-call-transcript) | `nebius/transcript.txt` |
| Reddit | [Q1 2026 transcript](https://cdn3.benzinga.com/insights/news/26/04/52201185/full-transcript-reddit-q1-2026-earnings-call) | `reddit/transcript.txt` |

The pilot's source list is saved as a search replay snapshot so its model and
acquisition test remains deterministic when consumer search pages throttle
automated traffic. In a normal run, the pipeline starts with Google and retries
only failed/empty queries with DuckDuckGo; it does not use pilot seeds.
