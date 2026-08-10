"""First-valid-transcript Pathfinder and Cosmos ingestion worker."""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from run_earnings_call_downstream import download_pdf, kind, opencli_extract, scrape_web, video_transcript
from valuechain.config import Settings
from valuechain.earnings_call_content import transcript_closing_signals, transcript_is_complete
from valuechain.earnings_calls import BLOCKED_TRANSCRIPT_DOMAINS
from valuechain.llm_client import LLMConfig, OpenAICompatibleClient, parse_json_content

COSMOS = "pi@100.102.250.107:/mnt/hdd8tb/valuechain/earnings_calls"
# Observed downloader failures with zero validated transcripts in this corpus.
# Keep Yahoo/Roic/Intellectia out of this list: they have demonstrated passes.
DOWNLOADER_BLOCKED_DOMAINS = BLOCKED_TRANSCRIPT_DOMAINS + (
    "investing.com", "marketbeat.com", "wallstreetzen.com", "kavout.com",
    "zoominfo.com", "enablx.com", "stockiq.tech", "stockanalysis.com",
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS pathfinder_transcripts (
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), accepted_url_id INTEGER NOT NULL REFERENCES accepted_urls(id),
  candidate_rank INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('validated','rejected','download_failed')),
  source_url TEXT NOT NULL, source_kind TEXT NOT NULL, local_path TEXT, cosmos_path TEXT,
  text_chars INTEGER, validation_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(company_id, accepted_url_id)
);
CREATE TABLE IF NOT EXISTS pathfinder_company_status (
  company_id INTEGER PRIMARY KEY REFERENCES companies(id), status TEXT NOT NULL CHECK(status IN ('validated','exhausted')),
  updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def db_connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000"); db.execute("PRAGMA journal_mode=WAL"); db.executescript(SCHEMA)
    return db


def normalize_text(raw: str) -> str:
    """Remove raw-HTML/script residue while retaining transcript paragraphs."""
    if raw.lstrip().startswith("%PDF"):
        raise ValueError("binary PDF reached postprocessor instead of pdftotext")
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", html.unescape(raw))
    raw = raw.replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def validation_prompt(company: str, year: int, quarter: str, text: str) -> tuple[str, str]:
    system = """You are a strict earnings-call transcript gate. Return only JSON. A valid FULL earnings call is spoken management/operator content for the target company and exact quarter/year, not merely financial results. Its opening normally includes an event header, operator/IR welcome, management names, safe-harbor or non-GAAP language, and then speaker-attributed prepared remarks. A full call normally reaches the end of Q&A and a closing statement/operator conclusion. Mark full_call=false and document_type=partial_call when the tail stops in the middle of prepared remarks, a question, or an answer, even when the opening and Q&A are genuine. Reject SEC filings (10-K, 10-Q, 8-K), annual reports, press releases, presentations, news summaries, transcript indexes, and pages that only link to a call. An SEC filing reference inside a call's safe-harbor disclaimer is not a rejection by itself. The opening excerpt is intentionally limited to 20,000 characters, but the tail excerpt is from the actual end of the extracted document; use it to detect truncation. Period matching is mandatory: a complete call for another quarter must have period_match=false and cannot be accepted."""
    lower = text.lower()
    qa_signals = [marker for marker in ("question-and-answer", "questions and answers", "q&a", "operator instructions", "analyst") if marker in lower]
    closing_signals = transcript_closing_signals(text)
    user = f"Target: {company}; {quarter} {year}. Document has {len(text)} normalized characters. Transcript-wide Q&A signals: {qa_signals or ['none found']}. Deterministically detected call-closing signals near the actual document end: {list(closing_signals) or ['none found']}. When these closing signals agree with the visible tail, do not claim that an earlier passage is the document ending.\nOpening window ({min(len(text), 20000)} chars):\n---\n{text[:20000]}\n---\nTail window ({min(len(text), 4000)} chars):\n---\n{text[-4000:]}\n---\nReturn {{\"full_call\":boolean,\"confidence\":0..1,\"document_type\":\"earnings_call|partial_call|filing|press_release|presentation|index|other\",\"period_match\":boolean,\"opening_signals\":[string],\"missing_or_problem\":string,\"reason\":string}}."
    return system, user


async def validate(company: str, year: int, quarter: str, text: str) -> dict:
    settings = Settings()
    client = OpenAICompatibleClient(LLMConfig(settings.llm_base_url, settings.llm_api_key, settings.complex_model, timeout_s=180))
    system, user = validation_prompt(company, year, quarter, text)
    try:
        raw = await client.chat_json_async(system, user, max_tokens=700)
    finally:
        await client.aclose()
    if not isinstance(raw, dict):
        raise ValueError("validator did not return a JSON object")
    return raw


async def obtain(client: httpx.AsyncClient, url: str, output: Path, profile: str, fallback_lock: asyncio.Lock) -> tuple[str, str]:
    source_kind = kind(url)
    if source_kind == "youtube":
        return await video_transcript(client, url)
    if source_kind == "pdf":
        return await asyncio.to_thread(download_pdf, url, output)
    try:
        return await scrape_web(client, url)
    except Exception:
        async with fallback_lock:
            return await asyncio.to_thread(opencli_extract, url, profile, f"earnings-pathfinder-{output.name}")


def conflicting_source_period(title: str, url: str, year: int, quarter: str) -> bool:
    """Cheap deterministic guard against an otherwise plausible wrong quarter."""
    haystack = f"{title} {url}".lower()
    explicit_years = set(re.findall(r"\b20\d{2}\b", haystack))
    # If a link identifies a calendar year, it must identify the target year.
    # (A year-less stable IR URL is still allowed and gets body-level Qwen
    # validation.)  This catches stale transcript archives such as Q1 2025.
    if explicit_years and str(year) not in explicit_years:
        return True
    if str(year) not in haystack:
        return False
    requested = quarter.lower()
    return any(token in haystack for token in ("q1", "q2", "q3", "q4") if token != requested)


def blocked_downloader_host(url: str) -> bool:
    host = re.sub(r"^www\.", "", urlparse(url).netloc.lower())
    return any(host == domain or host.endswith(f".{domain}") for domain in DOWNLOADER_BLOCKED_DOMAINS)


async def sync_to_cosmos(local_dir: Path, remote_dir: str) -> None:
    remote_path = f"/mnt/hdd8tb/valuechain/earnings_calls/{remote_dir}"
    created = await asyncio.to_thread(subprocess.run, ["ssh", "pi@100.102.250.107", "mkdir", "-p", remote_path], capture_output=True, text=True, timeout=60)
    if created.returncode:
        raise RuntimeError(created.stderr[-500:])
    completed = await asyncio.to_thread(subprocess.run, ["rsync", "-a", f"{local_dir}/", f"{COSMOS}/{remote_dir}/"], capture_output=True, text=True, timeout=180)
    if completed.returncode:
        raise RuntimeError(completed.stderr[-500:])


async def process_company(db: sqlite3.Connection, row: sqlite3.Row, year: int, quarter: str, output: Path, profile: str, client: httpx.AsyncClient, fallback_lock: asyncio.Lock, sync_cosmos: bool = True) -> None:
    company_id, ticker, company = row["id"], row["ticker"], row["company_name"]
    candidates = db.execute("SELECT a.id, a.url, a.title, a.content_kind, a.confidence FROM accepted_urls a WHERE a.company_id=? ORDER BY CASE a.content_kind WHEN 'official_transcript' THEN 0 WHEN 'third_party_transcript' THEN 1 WHEN 'official_webcast' THEN 2 WHEN 'youtube_video' THEN 3 ELSE 4 END, a.confidence DESC, a.id", (company_id,)).fetchall()
    for rank, candidate in enumerate(candidates, 1):
        target = output / ticker / str(candidate["id"]); target.mkdir(parents=True, exist_ok=True)
        try:
            if blocked_downloader_host(candidate["url"]):
                raise ValueError("source domain is blacklisted after repeated unreadable/paywall downloads")
            if conflicting_source_period(candidate["title"], candidate["url"], year, quarter):
                raise ValueError("source URL/title explicitly names a different quarter for target year")
            raw, fetch_method = await obtain(client, candidate["url"], target, profile, fallback_lock)
            # Some CDNs deliver a PDF at a URL without a .pdf suffix.  ULSCR
            # returns its magic header as text; switch to the binary path.
            if raw.lstrip().startswith("%PDF") and kind(candidate["url"]) == "web":
                raw, fetch_method = await asyncio.to_thread(download_pdf, candidate["url"], target)
            text = normalize_text(raw)
            # ULSCR can return a successful but very short consent/paywall shell.
            # Treat that as an extraction failure for ordinary web pages and use
            # the explicit authenticated OpenCLI fallback selected by the user.
            if len(text) < 2500 and kind(candidate["url"]) == "web":
                async with fallback_lock:
                    raw, fetch_method = await asyncio.to_thread(
                        opencli_extract, candidate["url"], profile, f"earnings-pathfinder-{output.name}"
                    )
                text = normalize_text(raw)
            if len(text) < 2500:
                raise RuntimeError(f"cleaned text too short: {len(text)}")
            verdict = await validate(f"{company} ({ticker})", year, quarter, text)
            initial_full = transcript_is_complete(
                verdict,
                text,
                source_kind=kind(candidate["url"]),
                content_kind=candidate["content_kind"],
            )
            # ULSCR can also return a substantial but capped excerpt.  If the
            # model sees genuine call content but the strict completeness gate
            # fails, give the same URL one paginated Browser Bridge attempt.
            # Keep the browser result only when it is materially longer.
            call_like = verdict.get("document_type") in {"earnings_call", "partial_call"}
            if (
                kind(candidate["url"]) == "web"
                and not initial_full
                and not fetch_method.startswith("opencli.")
                and call_like
            ):
                try:
                    async with fallback_lock:
                        browser_raw, browser_method = await asyncio.to_thread(
                            opencli_extract,
                            candidate["url"],
                            profile,
                            f"earnings-pathfinder-{output.name}",
                        )
                    browser_text = normalize_text(browser_raw)
                    if len(browser_text) > len(text) + 1_000:
                        text, fetch_method = browser_text, browser_method
                        verdict = await validate(f"{company} ({ticker})", year, quarter, text)
                except Exception:
                    # Preserve the primary extraction and its partial/rejected
                    # verdict when the browser fallback itself is unavailable.
                    pass
            transcript = target / "transcript.txt"; transcript.write_text(text, encoding="utf-8")
            metadata = {"ticker": ticker, "company_name": company, "year": year, "quarter": quarter, "accepted_url_id": candidate["id"], "source_url": candidate["url"], "source_kind": kind(candidate["url"]), "accepted_content_kind": candidate["content_kind"], "accepted_confidence": candidate["confidence"], "fetch_method": fetch_method, "text_chars": len(text), "validation": verdict}
            (target / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            # Qwen validates both document type and completeness. Deterministic
            # code may reject its answer, but must never promote partial or
            # wrong-period content into a full transcript.
            lower = text.lower()
            qa_present = any(marker in lower for marker in ("question-and-answer", "questions and answers", "q&a", "operator: this concludes", "reached the end of the question"))
            pathfinder_full = transcript_is_complete(
                verdict,
                text,
                source_kind=kind(candidate["url"]),
                content_kind=candidate["content_kind"],
            )
            verdict["pathfinder_full_call"] = pathfinder_full
            verdict["pathfinder_qa_present"] = qa_present
            metadata["validation"] = verdict
            (target / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            status = "validated" if pathfinder_full and float(verdict.get("confidence", 0)) >= .80 else "rejected"
            cosmos_dir = f"{year}/{quarter}/{ticker}/{candidate['id']}"
            if status == "validated":
                cosmos_path = None
                if sync_cosmos:
                    await sync_to_cosmos(target, cosmos_dir)
                    cosmos_path = f"/mnt/hdd8tb/valuechain/earnings_calls/{cosmos_dir}"
                db.execute("UPDATE pathfinder_transcripts SET status='rejected', updated_at=? WHERE company_id=? AND accepted_url_id<>? AND status='validated'", (now(), company_id, candidate["id"]))
                db.execute("INSERT OR REPLACE INTO pathfinder_transcripts(company_id, accepted_url_id, candidate_rank, status, source_url, source_kind, local_path, cosmos_path, text_chars, validation_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (company_id, candidate["id"], rank, status, candidate["url"], kind(candidate["url"]), str(transcript), cosmos_path, len(text), json.dumps(verdict), now(), now()))
                db.execute("INSERT OR REPLACE INTO pathfinder_company_status(company_id, status, updated_at) VALUES (?, 'validated', ?)", (company_id, now()))
                print(f"VALIDATED {ticker} rank={rank} chars={len(text)}", flush=True)
                return
            db.execute("INSERT OR REPLACE INTO pathfinder_transcripts(company_id, accepted_url_id, candidate_rank, status, source_url, source_kind, local_path, cosmos_path, text_chars, validation_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)", (company_id, candidate["id"], rank, status, candidate["url"], kind(candidate["url"]), str(transcript), len(text), json.dumps(verdict), now(), now()))
        except Exception as exc:
            db.execute("INSERT OR REPLACE INTO pathfinder_transcripts(company_id, accepted_url_id, candidate_rank, status, source_url, source_kind, local_path, cosmos_path, text_chars, validation_json, created_at, updated_at) VALUES (?, ?, ?, 'download_failed', ?, ?, NULL, NULL, NULL, ?, ?, ?)", (company_id, candidate["id"], rank, candidate["url"], kind(candidate["url"]), json.dumps({"error": f"{type(exc).__name__}: {exc}"}), now(), now()))
    db.execute("INSERT OR REPLACE INTO pathfinder_company_status(company_id, status, updated_at) VALUES (?, 'exhausted', ?)", (company_id, now()))
    print(f"NO_VALID_TRANSCRIPT {ticker}", flush=True)


async def main_async(args) -> None:
    db = db_connect(args.db)
    retry_filters = {
        "pending": "NOT EXISTS (SELECT 1 FROM pathfinder_company_status s WHERE s.company_id=c.id)",
        "exhausted": "EXISTS (SELECT 1 FROM pathfinder_company_status s WHERE s.company_id=c.id AND s.status='exhausted')",
        "partial": "EXISTS (SELECT 1 FROM pathfinder_transcripts p WHERE p.company_id=c.id AND p.status='validated' AND json_extract(p.validation_json, '$.document_type')='partial_call')",
        "validated-web": "EXISTS (SELECT 1 FROM pathfinder_transcripts p WHERE p.company_id=c.id AND p.status='validated' AND p.source_kind='web')",
    }
    retry_filter = retry_filters[args.retry_mode]
    if args.tickers:
        tickers = [item.strip().upper() for item in args.tickers.split(",") if item.strip()]
        placeholders = ",".join("?" for _ in tickers)
        rows = db.execute(f"SELECT c.* FROM companies c WHERE c.ticker IN ({placeholders}) AND EXISTS (SELECT 1 FROM accepted_urls a WHERE a.company_id=c.id) AND {retry_filter} ORDER BY c.id", tickers).fetchall()
    else:
        rows = db.execute(f"SELECT c.* FROM companies c WHERE c.status='completed' AND EXISTS (SELECT 1 FROM accepted_urls a WHERE a.company_id=c.id) AND {retry_filter} ORDER BY c.id LIMIT ?", (args.limit,)).fetchall()
    print(f"selected={len(rows)} retry_mode={args.retry_mode}", flush=True)
    fallback_lock = asyncio.Lock(); semaphore = asyncio.Semaphore(args.workers)
    async with httpx.AsyncClient() as client:
        async def guarded(row):
            async with semaphore:
                await process_company(db, row, args.year, args.quarter, args.output_dir, args.opencli_profile, client, fallback_lock, not args.skip_cosmos)
        await asyncio.gather(*(guarded(row) for row in rows))
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--year", type=int, default=2026); parser.add_argument("--quarter", default="Q1"); parser.add_argument("--workers", type=int, default=4); parser.add_argument("--limit", type=int, default=20); parser.add_argument("--tickers"); parser.add_argument("--opencli-profile", default="3h6e2wgv")
    parser.add_argument(
        "--retry-mode",
        choices=("pending", "exhausted", "partial", "validated-web"),
        default="pending",
        help="Select never-processed companies, exhausted companies, or previously misclassified partial calls.",
    )
    parser.add_argument("--skip-cosmos", action="store_true", help="Pilot mode: keep validated artifacts local and do not sync them to Cosmos.")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
