"""Download accepted earnings-call links through Endeavor's transcript/scrape services."""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import requests

from valuechain.earnings_calls import USER_AGENT, _rotating_proxy
from valuechain.earnings_call_content import parse_opencli_extract_chunk
from valuechain.earnings_call_artifacts import compress_artifact_directory

VIDEO_URL = "http://100.114.26.88:13131/transcript"
ULSCR_URL = "http://100.114.26.88:23355"

SCHEMA = """
CREATE TABLE IF NOT EXISTS downstream_downloads (
  accepted_url_id INTEGER PRIMARY KEY REFERENCES accepted_urls(id), source_url TEXT NOT NULL,
  source_kind TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','downloaded','failed')),
  artifact_path TEXT, text_chars INTEGER, fetch_method TEXT, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_downstream_status ON downstream_downloads(status);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def kind(url: str) -> str:
    lowered = url.lower().split("?", 1)[0]
    if "youtube.com/" in lowered or "youtu.be/" in lowered:
        return "youtube"
    if lowered.endswith(".pdf"):
        return "pdf"
    return "web"


def conn(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


async def video_transcript(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    response = await client.post(VIDEO_URL, json={"url": url, "languages": ["en", "en-US"]}, timeout=120)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success") or not payload.get("transcript_text"):
        raise RuntimeError(payload.get("warning") or "video service returned no transcript")
    return str(payload["transcript_text"]), "video.transcript"


def download_pdf(url: str, directory: Path) -> tuple[str, str]:
    proxies = _rotating_proxy()
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, proxies=proxies, timeout=60)
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        raise RuntimeError("PDF URL did not return a PDF")
    pdf = directory / "source.pdf"; pdf.write_bytes(response.content)
    text = directory / "transcript.txt"
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext is unavailable")
    completed = subprocess.run(["pdftotext", "-layout", str(pdf), str(text)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    if completed.returncode or not text.exists() or text.stat().st_size < 500:
        raise RuntimeError("PDF text extraction failed")
    return text.read_text(encoding="utf-8", errors="ignore"), "pdf.pdftotext"


def opencli_extract(
    url: str,
    profile: str,
    session: str,
    *,
    chunk_size: int = 20_000,
    max_chars: int = 1_000_000,
) -> tuple[str, str]:
    """Last-resort browser extraction; profile/session must always be explicit."""
    if not profile:
        raise RuntimeError("OpenCLI fallback requires --opencli-profile")
    base = ["opencli", "--profile", profile, "browser", session]
    # The caller provides a registered Browser Bridge profile; no implicit/default
    # profile or temporary browser profile is ever created here.
    # Keep the diagnostic call for auditability, but Browser Bridge can still be
    # usable when `doctor` reports the known multi-profile warning. The actual
    # explicit-profile open below is the authoritative connectivity check.
    subprocess.run(["opencli", "--profile", profile, "doctor"], capture_output=True, text=True, timeout=30)
    opened = subprocess.run(base + ["open", url, "--window", "background"], capture_output=True, text=True, timeout=60)
    if opened.returncode:
        raise RuntimeError(opened.stderr[-500:] or "OpenCLI could not open the page")
    try:
        # `open` returns once navigation starts.  Give client-rendered
        # transcript pages a deterministic settling window before extraction;
        # extracting immediately was producing empty shells on Yahoo and IR
        # sites even when the same page became readable moments later.
        subprocess.run(base + ["wait", "time", "3"], capture_output=True, text=True, timeout=15)
        chunks: list[str] = []
        start = 0
        while True:
            extracted = subprocess.run(
                base + ["extract", "--chunk-size", str(chunk_size), "--start", str(start)],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if extracted.returncode or not extracted.stdout.strip():
                raise RuntimeError(extracted.stderr[-500:] or "OpenCLI extraction was empty")
            try:
                chunk = parse_opencli_extract_chunk(extracted.stdout)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise RuntimeError(f"OpenCLI extraction metadata was invalid: {exc}") from exc
            if chunk.start != start:
                raise RuntimeError(
                    f"OpenCLI extraction cursor mismatch: requested {start}, got {chunk.start}"
                )
            chunks.append(chunk.content)
            assembled_chars = sum(len(part) for part in chunks)
            if assembled_chars > max_chars:
                raise RuntimeError(f"OpenCLI extraction exceeded {max_chars} characters")
            if chunk.next_start_char is None:
                break
            start = chunk.next_start_char
        text = "\n".join(chunks)
        if len(text) < 500:
            raise RuntimeError("OpenCLI extraction did not contain readable content")
        return text, "opencli.browser_extract.paginated"
    finally:
        subprocess.run(base + ["close"], capture_output=True, text=True, timeout=30)


async def scrape_web(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    request = {"urls": [url], "min_chars": 500, "use_proxy": True, "return_markdown": False, "fallbacks": {"naive": True, "headless": True, "head": False, "jina": True}}
    submitted = (await client.post(f"{ULSCR_URL}/scrape_sync", json=request, timeout=60)).json()
    job_id = submitted["results"][0]["job_id"]
    for _ in range(30):
        await asyncio.sleep(1)
        payload = (await client.post(f"{ULSCR_URL}/results_batch", json={"job_ids": [job_id], "include_text": True, "include_attempts": True}, timeout=60)).json()
        item = payload["results"][0]
        if item["status"] == "complete":
            result = item["result"]
            if result.get("success") and len(result.get("text", "")) >= 500:
                return str(result["text"]), f"ulscr.{result.get('selected_attempt', result.get('method', 'unknown'))}"
            raise RuntimeError(result.get("error_type") or "ULSCR returned insufficient text")
        if item["status"] == "failed":
            raise RuntimeError(str(item))
    raise RuntimeError("ULSCR polling timed out")


async def process(client: httpx.AsyncClient, db: sqlite3.Connection, row: sqlite3.Row, output: Path, opencli_profile: str, fallback_lock: asyncio.Lock, force_opencli: bool = False) -> None:
    link_id, url, source_kind = row["id"], row["url"], kind(row["url"])
    target = output / str(link_id); target.mkdir(parents=True, exist_ok=True)
    db.execute("INSERT OR REPLACE INTO downstream_downloads(accepted_url_id, source_url, source_kind, status, created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?)", (link_id, url, source_kind, now(), now()))
    try:
        if force_opencli and source_kind == "web":
            raise RuntimeError("development: forced ULSCR failure")
        if source_kind == "youtube":
            text, method = await video_transcript(client, url)
        elif source_kind == "pdf":
            text, method = await asyncio.to_thread(download_pdf, url, target)
        else:
            text, method = await scrape_web(client, url)
        artifact = target / "transcript.txt"; artifact.write_text(text, encoding="utf-8")
        artifact = compress_artifact_directory(target)[artifact]
        db.execute("UPDATE downstream_downloads SET status='downloaded', artifact_path=?, text_chars=?, fetch_method=?, error=NULL, updated_at=? WHERE accepted_url_id=?", (str(artifact), len(text), method, now(), link_id))
        print(f"downloaded {link_id} {source_kind} {len(text)} chars", flush=True)
    except Exception as primary_error:
        # OpenCLI is only the final fallback for ULSCR web-page fetches. Video
        # uses the dedicated transcript service and PDFs use binary download +
        # pdftotext; neither should open a browser page as a substitute.
        if source_kind != "web":
            compress_artifact_directory(target)
            db.execute("UPDATE downstream_downloads SET status='failed', error=?, updated_at=? WHERE accepted_url_id=?", (f"{type(primary_error).__name__}: {primary_error}", now(), link_id))
            print(f"FAILED {link_id} {source_kind}: {primary_error}", flush=True)
            return
        try:
            # Browser fallback is deliberately serialized to avoid competing for
            # the same explicit Browser Bridge profile/session.
            async with fallback_lock:
                text, method = await asyncio.to_thread(opencli_extract, url, opencli_profile, f"earnings-{link_id}")
            artifact = target / "transcript.txt"; artifact.write_text(text, encoding="utf-8")
            artifact = compress_artifact_directory(target)[artifact]
            db.execute("UPDATE downstream_downloads SET status='downloaded', artifact_path=?, text_chars=?, fetch_method=?, error=?, updated_at=? WHERE accepted_url_id=?", (str(artifact), len(text), method, f"primary_failed: {type(primary_error).__name__}: {primary_error}", now(), link_id))
            print(f"downloaded {link_id} via OpenCLI", flush=True)
        except Exception as fallback_error:
            compress_artifact_directory(target)
            db.execute("UPDATE downstream_downloads SET status='failed', error=?, updated_at=? WHERE accepted_url_id=?", (f"primary={type(primary_error).__name__}: {primary_error}; opencli={type(fallback_error).__name__}: {fallback_error}", now(), link_id))
            print(f"FAILED {link_id} {source_kind}: {primary_error}; OpenCLI: {fallback_error}", flush=True)


async def main_async(args) -> None:
    db = conn(args.db)
    if args.accepted_url_ids:
        ids = [int(value) for value in args.accepted_url_ids.split(",")]
        placeholders = ",".join("?" for _ in ids)
        rows = db.execute(f"SELECT id, url FROM accepted_urls WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
    else:
        rows = db.execute("SELECT a.id, a.url FROM accepted_urls a LEFT JOIN downstream_downloads d ON d.accepted_url_id=a.id WHERE d.accepted_url_id IS NULL OR d.status='failed' ORDER BY a.id LIMIT ?", (args.limit,)).fetchall()
    semaphore = asyncio.Semaphore(args.workers)
    fallback_lock = asyncio.Lock()
    async with httpx.AsyncClient() as client:
        async def guarded(row):
            async with semaphore:
                await process(client, db, row, args.output_dir, args.opencli_profile, fallback_lock, args.force_opencli)
        await asyncio.gather(*(guarded(row) for row in rows))
    print(dict(db.execute("SELECT status, COUNT(*) FROM downstream_downloads GROUP BY status").fetchall()))
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20); parser.add_argument("--workers", type=int, default=4); parser.add_argument("--accepted-url-ids"); parser.add_argument("--opencli-profile", default=os.getenv("VALUECHAIN_OPENCLI_PROFILE", "")); parser.add_argument("--force-opencli", action="store_true", help="development-only: exercise web fallback without calling ULSCR")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
