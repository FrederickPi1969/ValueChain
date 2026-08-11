# Earnings-call collection pipeline

## Search rule table

| Field | Rules used |
|---|---|
| Quarter | `Q1`, `q1`, `first quarter`, `first-quarter`, `first quarterly` (and corresponding second/third/fourth forms) |
| Year | `2026`, `FY26`, `FY 26`, `fiscal 2026`, `fiscal year 2026` |
| Event phrase | `earnings call transcript`, `earnings call`, `results webcast`, `results conference call`, `financial results briefing`, `investor update`, `results announcement webcast`, `analyst conference` |
| Query | `"{company}" "{quarter variant}" "{year variant}" "{event phrase}"` |
| Search order | `http://100.114.26.88:10087/search` first (Endeavor over Tailscale; Google CSE key pool); only if it returns no result or errors, retry that exact query on DuckDuckGo HTML. |

The cross-product remains available for exploratory research, but the production collector uses the following four ordered best bets and executes at most four requests:

1. `company + Q1 + 2026 + earnings call transcript`
2. `company + first quarter + 2026 + earnings call transcript`
3. `company + Q1 + FY26 + earnings call transcript`
4. `company + Q1 + 2026 + earnings call webcast`

After every query, Qwen judges that query's full result set. A qualifying link with confidence >= 0.90 ends the company search immediately. Otherwise the next query runs, up to the four-query cap.
The Google SERP endpoint remains the private Tailnet address, `100.114.26.88:10087`. Its Endeavor-side `GoogleCSEClient` rotates a proxy for every **upstream Google CSE request**, so Google egress is proxied while the client-to-Endeavor hop stays private (putting a public proxy on the `100.x` hop returns 502). External candidate pages and the DuckDuckGo emergency fallback use the collector's proxy. Proxy credentials remain only in process memory; an explicitly configured `VALUECHAIN_HTTP_PROXY` / `VALUECHAIN_HTTPS_PROXY` overrides the rotating pool. The Google service itself pools CSE keys and returns 429 with `Retry-After` when all keys are cooling down; callers should defer rather than retry-burst.

## Acceptance and acquisition

`valuechain.earnings_calls` sends all candidate title/URL/snippet fields to local `Qwen/Qwen3.6-35B-A3B` with thinking disabled, temperature 0, and a strict JSON schema. It accepts only confidence >= 0.70 results classified as an official/third-party transcript, official webcast, or YouTube video. A deterministic guard excludes 10-K, 10-Q, 8-K, annual-report, and EDGAR links even if the model makes an error. Every result from every executed query is retained in `manifest.json`, whether it is accepted, rejected, a YouTube URL, PDF, webcast, or unrelated link.

HTML candidates are archived as normalized text. YouTube captions come from the dedicated transcript service, and PDF calls are converted with `pdftotext`. Before an artifact is recorded or synchronized to Cosmos, every `transcript.txt`, `metadata.json`, and retained `source.pdf` is compressed independently with Zstandard level 10 and verified with `zstd -t`. Only the resulting `*.zst` file remains; SQLite paths point directly to `transcript.txt.zst`. This keeps each component stream-readable without unpacking a tar archive. Use `zstd -d -c artifact.txt.zst` to read one artifact.

Browser extraction follows OpenCLI's `next_start_char` cursor until the full page is retrieved. Web transcripts must pass Qwen's exact-period/full-call judgement and contain a call-ending signal near the actual document boundary. A partial call, a different quarter, or a browser extraction that stops before `total_chars` is rejected.

Run one company:

```bash
PYTHONPATH=src python3 -m valuechain.earnings_calls Tesla --year 2026 --quarter Q1 \
  --output-dir data/earnings_calls/tesla_2026_q1
```
