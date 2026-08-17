# Earnings-call downloader v2

## Contract

The downloader consumes rows from `accepted_urls`; it does not decide that a
link is a complete earnings call.  Its output is cleaned, integrity-checked
text for the Pathfinder/Qwen validator.

Source routing is fixed:

1. YouTube URLs go to the Endeavor video transcript service.
2. PDF URLs use a rotating upstream proxy, PDF magic-byte validation, a 100 MiB
   bound, and `pdftotext`.
3. Ordinary web pages go to the internal ULSCAR service with `use_proxy=true`.
4. Only a failed/invalid ordinary web extraction goes to OpenCLI.  Every
   OpenCLI browser command runs on `macmini-m4`; there is no local-browser mode.

Every extracted document passes the same postprocessor.  It removes HTML,
scripts, Markdown images, inline data URIs, cursor-split base64 payloads and
navigation URLs.  A deterministic quality gate rejects short/non-text/image-
polluted output before it can be recorded as downloaded.

## Worker reliability

`scripts/run_earnings_call_downstream.py` maintains:

- an atomic SQLite claim;
- `lease_owner`, lease expiry and heartbeat;
- stale-lease recovery;
- bounded attempts and exponential retry timestamps;
- one connection per async worker;
- an append-only attempt record;
- per-attempt staging directories;
- Zstandard integrity tests;
- an immutable bundle manifest containing compressed-file size and SHA-256;
- a final manifest/text/hash/quality audit before the database commit;
- one same-filesystem directory rename into a unique immutable local version;
- a bounded repair budget for legacy or damaged artifacts.

The lease is rechecked immediately before promotion. A crashed or stale worker
cannot overwrite another worker's artifact. If the directory rename succeeds
but the following SQLite transaction fails, the only residue is a complete,
unreferenced version that a later garbage collector can remove.

Compression uses two threads per file, not `zstd -T0`, so eight download
workers cannot each claim every CPU core.

Example:

```bash
PYTHONPATH=src python3 scripts/run_earnings_call_downstream.py \
  --db data/earnings_calls/quarter.sqlite3 \
  --output-dir data/earnings_calls/downloader/2026/Q1 \
  --workers 8 --limit 1000
```

`--audit-existing` verifies the v2 manifest, all Zstandard frames, compressed
file hashes, transcript hash, text length and quality gates.  Legacy or damaged
artifacts become retryable instead of being silently trusted.

## Remote OpenCLI

Mac mini configuration:

```text
SSH host: macmini-m4
OpenCLI: /opt/homebrew/bin/opencli (1.8.6)
Helper: /Users/frederickpi/.local/bin/valuechain-opencli-extract
Profile selector: auto-single
```

The helper is one SSH operation per URL.  On the Mac mini it resolves exactly
one connected Browser Bridge profile, then passes the concrete profile on every
browser command.  It holds a profile-wide file lock, enforces one overall
deadline, joins character chunks without inserting bytes, explicitly closes
the target tab, releases the session lease, and returns cleanup warnings as a
hard failure. It also requires every chunk's offsets to equal its actual text
length and the final assembled length to equal OpenCLI's reported total.
`auto-single` fails if zero or multiple profiles are connected.

The remote daemon is managed by
`~/Library/LaunchAgents/ai.opencli.daemon.plist` with `RunAtLoad` and
`KeepAlive`.  Killing the daemon and observing automatic recovery is part of
the deployment smoke test.  The worker never changes Chrome extensions,
profiles or login state.

## 2026-08-11 live pilots

The pilot exercised all three acquisition routes and then ran a clean restart
audit:

| Route | Result | Clean characters |
|---|---|---:|
| Remote OpenCLI / Microsoft IR | downloaded | 52,947 |
| YouTube transcript service | downloaded | 103,920 |
| PDF / SMFG conference PDF | downloaded | 9,646 |

The final atomic pilot repeated these three routes with three concurrent
workers. All three succeeded on attempt one. Each bundle is under a unique
`versions/attempt-<n>-<uuid>/` directory and contains `transcript.txt.zst`,
`metadata.json.zst` and `manifest.json.zst`; the PDF bundle also contains
`source.pdf.zst`. Every ZSTD frame, exact manifest membership, candidate ID,
file hash and transcript hash passed a clean restart `--audit-existing` run
(`valid_v2=3`, `invalid=0`).

A separate remote-helper smoke extracted all 55,077 reported characters from
Microsoft IR, returned `cleanup_warnings=[]`, and left the managed tab list
empty. The deployed helper hash matches the reviewed local file.

Eleven obsolete, submitted `com.valuechain.earnings.*` launchd jobs were also
removed from the workstation. Some were erroneous one-shot commands configured
with KeepAlive and had been restarted more than 40,000 times. ValueChain now
has no loaded local earnings launch job; unrelated Moonbow/OpenCLI services
were not changed.

Validated bundles are published with the transactional Cosmos publisher. It
binds year, quarter, ticker and candidate ID, verifies the remote bytes and
ZSTD frames, promotes an immutable version, and changes `current` last with an
atomic symlink replacement. It never uses `rsync --delete`.

## Existing-corpus debt

The old Pathfinder corpus must not be treated as fully clean yet:

- 296 of 507 currently validated transcripts contain Markdown/data-URI image
  pollution from the legacy OpenCLI join logic. Of those, 236 retain a closing
  signal and can be sanitized then revalidated; 60 require refetch because the
  cleaned text no longer has a reliable call boundary.
- Cosmos contains 261 candidate directories that are no longer current (96
  demoted rows and 165 untracked directories).  They should be quarantined by
  a manifest-aware garbage collector, not deleted ad hoc.
- Historical Pathfinder and the downloader duplicated fetching.  New batch
  work should download once through v2, validate locally, then publish through
  an atomic Cosmos publisher.
