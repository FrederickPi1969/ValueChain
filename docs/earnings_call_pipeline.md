# Earnings-call collection pipeline

## Search rule table

| Field | Rules used |
|---|---|
| Quarter | `Q1`, `q1`, `first quarter`, `first-quarter`, `first quarterly` (and corresponding second/third/fourth forms) |
| Year | `2026`, `FY26`, `FY 26`, `fiscal 2026`, `fiscal year 2026` |
| Event phrase | `earnings call transcript`, `earnings call`, `results webcast`, `results conference call`, `financial results briefing`, `investor update`, `results announcement webcast`, `analyst conference` |
| Query | `"{company}" "{quarter variant}" "{year variant}" "{event phrase}"` |
| Search backends | Google uses `http://100.114.26.88:10087/search` over Tailscale. DuckDuckGo uses the task API at `https://serp.frederickpi.com`. Experiments must pin one backend; they must not use the legacy function that silently changes engines. |

The cross-product remains available for exploratory research, but the production collector uses the following four ordered best bets and executes at most four requests:

1. `ticker + company + 2026 + Q1 + earnings conference call`
2. `ticker + company + 2026 + Q1 + earnings conference call + YouTube`
3. `ticker + company + 2026 + Q1 + earnings call transcript`
4. `ticker + company + 2026 + Q1 + quarterly results conference call`

After every query, Qwen judges that query's full result set and every result is
retained.  A plausible/high-confidence link is not a successful search by
itself: the downloader must obtain it and the strict content validator must
confirm the requested period and a complete call.  Only that event ends the
company/engine arm; otherwise the next query runs, up to the four-query cap.
The Google SERP endpoint remains the private Tailnet address, `100.114.26.88:10087`. Its Endeavor-side `GoogleCSEClient` rotates a proxy for every **upstream Google CSE request**, so Google egress is proxied while the client-to-Endeavor hop stays private (putting a public proxy on the `100.x` hop returns 502). External candidate pages and the DuckDuckGo emergency fallback use the collector's proxy. Proxy credentials remain only in process memory; an explicitly configured `VALUECHAIN_HTTP_PROXY` / `VALUECHAIN_HTTPS_PROXY` overrides the rotating pool. The Google service itself pools CSE keys and returns 429 with `Retry-After` when all keys are cooling down; callers should defer rather than retry-burst.

## Acceptance and acquisition

`valuechain.earnings_calls` sends all candidate title/URL/snippet fields to local `Qwen/Qwen3.6-35B-A3B` with thinking disabled, temperature 0, and a strict JSON schema. It accepts only confidence >= 0.70 results classified as an official/third-party transcript, official webcast, or YouTube video. A deterministic guard excludes 10-K, 10-Q, 8-K, annual-report, and EDGAR links even if the model makes an error. Every result from every executed query is retained in `manifest.json`, whether it is accepted, rejected, a YouTube URL, PDF, webcast, or unrelated link.

HTML candidates go to ULSCAR first; OpenCLI is the final web-only fallback and
runs remotely on `macmini-m4`. YouTube captions come from the dedicated
transcript service, and PDF calls are magic-byte checked and converted with
`pdftotext`. All routes share the same HTML/Markdown/data-URI sanitizer and
quality gate. Before an artifact is recorded, every retained file is compressed
independently with Zstandard, verified with `zstd -t`, and covered by a bundle
manifest containing size and SHA-256. SQLite paths point directly to
`transcript.txt.zst`. See `docs/earnings_call_downloader_v2.md` for the worker,
lease, retry and remote-browser contract.

Browser extraction follows OpenCLI's `next_start_char` cursor until the full page is retrieved. Web transcripts must pass Qwen's exact-period/full-call judgement and contain a call-ending signal near the actual document boundary. A partial call, a different quarter, or a browser extraction that stops before `total_chars` is rejected.

Run one company:

```bash
PYTHONPATH=src python3 -m valuechain.earnings_calls Tesla --year 2026 --quarter Q1 \
  --output-dir data/earnings_calls/tesla_2026_q1
```
