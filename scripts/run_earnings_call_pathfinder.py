"""First-valid-transcript Pathfinder and Cosmos ingestion worker."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx
from run_earnings_call_downstream import (
    download_pdf,
    kind,
    opencli_extract,
    scrape_web,
    video_transcript,
)

from valuechain.config import Settings
from valuechain.earnings_call_artifacts import compress_artifact_directory
from valuechain.earnings_call_content import (
    normalize_transcript_text,
    transcript_closing_signals,
    transcript_is_complete,
    transcript_quality_problems,
)
from valuechain.earnings_call_publisher import (
    ARTIFACT_SCHEMA_VERSION,
    CosmosPublisherConfig,
    EarningsCallArtifactKey,
    EarningsCallPublisher,
    PublishError,
    PublishResult,
    validate_artifact_bundle,
)
from valuechain.earnings_calls import BLOCKED_TRANSCRIPT_DOMAINS
from valuechain.llm_client import LLMConfig, OpenAICompatibleClient

# Observed downloader failures with zero validated transcripts in this corpus.
# Keep Yahoo/Roic/Intellectia out of this list: they have demonstrated passes.
DOWNLOADER_BLOCKED_DOMAINS = BLOCKED_TRANSCRIPT_DOMAINS + (
    "investing.com", "marketbeat.com", "wallstreetzen.com", "kavout.com",
    "zoominfo.com", "enablx.com", "stockiq.tech", "stockanalysis.com",
)
VALIDATION_PROMPT_VERSION = "earnings-full-call-v2-grounded-dates"
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
    return normalize_transcript_text(raw)


def validation_prompt(company: str, year: int, quarter: str, text: str) -> tuple[str, str]:
    system = """You are a strict earnings-call transcript gate. Return only JSON. A valid FULL earnings call is spoken management/operator content for the target company and exact quarter/year, not merely financial results. Its opening normally includes an event header, operator/IR welcome, management names, safe-harbor or non-GAAP language, and then speaker-attributed prepared remarks. A full call normally reaches the end of Q&A and a closing statement/operator conclusion. Mark full_call=false and document_type=partial_call when the tail stops in the middle of prepared remarks, a question, or an answer, even when the opening and Q&A are genuine. Reject SEC filings (10-K, 10-Q, 8-K), annual reports, press releases, presentations, news summaries, transcript indexes, and pages that only link to a call. An SEC filing reference inside a call's safe-harbor disclaimer is not a rejection by itself. The opening excerpt is intentionally limited to 20,000 characters, but the tail excerpt is from the actual end of the extracted document; use it to detect truncation. Period matching is mandatory: a complete call for another quarter must have period_match=false and cannot be accepted. Extract fiscal_year, fiscal_quarter, reported_period_label, period_end, and call_date only when supported by the displayed excerpts. Use YYYY-MM-DD for dates. Never infer a date from the target, today's date, a filing reference, or normal quarter conventions; return null when the exact date is not stated. Do not confuse the financial period end with the call date."""
    lower = text.lower()
    qa_signals = [marker for marker in ("question-and-answer", "questions and answers", "q&a", "operator instructions", "analyst") if marker in lower]
    closing_signals = transcript_closing_signals(text)
    user = f"Target: {company}; {quarter} {year}. Document has {len(text)} normalized characters. Transcript-wide Q&A signals: {qa_signals or ['none found']}. Deterministically detected call-closing signals near the actual document end: {list(closing_signals) or ['none found']}. When these closing signals agree with the visible tail, do not claim that an earlier passage is the document ending.\nOpening window ({min(len(text), 20000)} chars):\n---\n{text[:20000]}\n---\nTail window ({min(len(text), 4000)} chars):\n---\n{text[-4000:]}\n---\nReturn compact JSON with at most 8 opening signals, at most 30 words in missing_or_problem, and at most 80 words in reason: {{\"full_call\":boolean,\"confidence\":0..1,\"document_type\":\"earnings_call|partial_call|filing|press_release|presentation|index|other\",\"period_match\":boolean,\"fiscal_year\":integer|null,\"fiscal_quarter\":\"Q1|Q2|Q3|Q4\"|null,\"reported_period_label\":string|null,\"period_end\":\"YYYY-MM-DD\"|null,\"call_date\":\"YYYY-MM-DD\"|null,\"opening_signals\":[string],\"missing_or_problem\":string,\"reason\":string}}."
    return system, user


_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def grounded_iso_date(value: object, text: str) -> str | None:
    """Accept an ISO date only when the transcript visibly supports it."""
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    year, month, day = parsed.year, parsed.month, parsed.day
    month_name = _MONTH_NAMES[month - 1]
    month_abbr = month_name[:3]
    ordinal = "th" if 10 < day % 100 < 14 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    visible = re.sub(r"\s+", " ", text.lower())
    forms = {
        parsed.isoformat(),
        f"{year}/{month:02d}/{day:02d}",
        f"{month:02d}/{day:02d}/{year}",
        f"{month}/{day}/{year}",
        f"{month_name} {day}, {year}",
        f"{month_abbr} {day}, {year}",
        f"{month_name} {day}{ordinal}, {year}",
        f"{day} {month_name} {year}",
        f"{day} {month_abbr} {year}",
    }
    return parsed.isoformat() if any(form in visible for form in forms) else None


def normalize_validation_metadata(raw: dict, text: str) -> dict:
    """Make optional period metadata typed, stable, and evidence-grounded."""
    normalized = dict(raw)
    fiscal_year = normalized.get("fiscal_year")
    if isinstance(fiscal_year, bool):
        fiscal_year = None
    try:
        fiscal_year = int(fiscal_year) if fiscal_year is not None else None
    except (TypeError, ValueError):
        fiscal_year = None
    normalized["fiscal_year"] = (
        fiscal_year if fiscal_year is not None and 1900 <= fiscal_year <= 2200 else None
    )
    fiscal_quarter = normalized.get("fiscal_quarter")
    fiscal_quarter = (
        str(fiscal_quarter).upper() if fiscal_quarter is not None else None
    )
    normalized["fiscal_quarter"] = (
        fiscal_quarter if fiscal_quarter in {"Q1", "Q2", "Q3", "Q4"} else None
    )
    label = normalized.get("reported_period_label")
    normalized["reported_period_label"] = (
        str(label).strip()[:200] if label is not None and str(label).strip() else None
    )
    normalized["period_end"] = grounded_iso_date(normalized.get("period_end"), text)
    normalized["call_date"] = grounded_iso_date(normalized.get("call_date"), text)
    return normalized


async def validate(company: str, year: int, quarter: str, text: str) -> dict:
    settings = Settings()
    client = OpenAICompatibleClient(LLMConfig(settings.llm_base_url, settings.llm_api_key, settings.complex_model, timeout_s=180))
    system, user = validation_prompt(company, year, quarter, text)
    last_error: Exception | None = None
    try:
        for attempt in range(3):
            retry_instruction = (
                ""
                if attempt == 0
                else "\nYour previous response was invalid or truncated. Return one compact, complete JSON object only."
            )
            try:
                raw = await client.chat_json_async(
                    system,
                    user + retry_instruction,
                    max_tokens=1200,
                )
                if not isinstance(raw, dict):
                    raise TypeError("validator did not return a JSON object")
                normalized = normalize_validation_metadata(raw, text)
                if normalized.get("period_match") is True:
                    # The model has affirmed the target label; make that label
                    # authoritative when calendar wording such as "December
                    # 2025 quarter" tempts it to emit the wrong fiscal year.
                    normalized["fiscal_year"] = year
                    normalized["fiscal_quarter"] = quarter.upper()
                return normalized
            except (json.JSONDecodeError, TypeError) as exc:
                last_error = exc
    finally:
        await client.aclose()
    raise RuntimeError("validator returned invalid JSON three times") from last_error


async def obtain(client: httpx.AsyncClient, url: str, output: Path, profile: str, host: str, fallback_lock: asyncio.Lock) -> tuple[str, str]:
    source_kind = kind(url)
    if source_kind == "youtube":
        return await video_transcript(client, url)
    if source_kind == "pdf":
        return await asyncio.to_thread(download_pdf, url, output)
    try:
        return await scrape_web(client, url)
    except Exception:  # noqa: BLE001 - ULSCAR failures route to the final browser extractor
        async with fallback_lock:
            return await asyncio.to_thread(
                opencli_extract,
                url,
                profile,
                f"earnings-pathfinder-{output.name}",
                host=host,
            )


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


class ArtifactPublisher(Protocol):
    """Narrow injection boundary used by the Pathfinder and its tests."""

    def publish(
        self, directory: Path, key: EarningsCallArtifactKey
    ) -> PublishResult: ...


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finalize_v2_bundle(
    directory: Path,
    metadata: dict,
    text: str,
    accepted_url_id: int,
) -> Path:
    """Materialize and independently validate one compressed v2 bundle."""
    text_bytes = text.encode("utf-8")
    metadata.update(
        {
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "text_chars": len(text),
            "text_sha256": hashlib.sha256(text_bytes).hexdigest(),
        }
    )
    transcript = directory / "transcript.txt"
    transcript.write_bytes(text_bytes)
    (directory / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    compressed = compress_artifact_directory(directory)
    compressed_transcript = compressed.get(
        transcript, directory / "transcript.txt.zst"
    )
    manifest = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "accepted_url_id": accepted_url_id,
        "created_at": now(),
        "files": {
            path.name: {
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
            for path in sorted(directory.iterdir())
            if path.is_file()
            and path.suffix == ".zst"
            and path.name != "manifest.json.zst"
        },
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    compress_artifact_directory(directory)
    validate_artifact_bundle(
        directory,
        EarningsCallArtifactKey(
            int(metadata["year"]),
            str(metadata["quarter"]),
            str(metadata["ticker"]),
            accepted_url_id,
        ),
    )
    return compressed_transcript


async def process_company(db: sqlite3.Connection, row: sqlite3.Row, year: int, quarter: str, output: Path, profile: str, host: str, client: httpx.AsyncClient, fallback_lock: asyncio.Lock, publisher: ArtifactPublisher | None = None) -> None:
    company_id, ticker, company = row["id"], row["ticker"], row["company_name"]
    candidates = db.execute("SELECT a.id, a.url, a.title, a.content_kind, a.confidence FROM accepted_urls a WHERE a.company_id=? ORDER BY CASE a.content_kind WHEN 'official_transcript' THEN 0 WHEN 'third_party_transcript' THEN 1 WHEN 'official_webcast' THEN 2 WHEN 'youtube_video' THEN 3 ELSE 4 END, a.confidence DESC, a.id", (company_id,)).fetchall()
    for rank, candidate in enumerate(candidates, 1):
        target = output / ticker / str(candidate["id"]); target.mkdir(parents=True, exist_ok=True)
        try:
            if blocked_downloader_host(candidate["url"]):
                raise ValueError("source domain is blacklisted after repeated unreadable/paywall downloads")
            if conflicting_source_period(candidate["title"], candidate["url"], year, quarter):
                raise ValueError("source URL/title explicitly names a different quarter for target year")
            raw, fetch_method = await obtain(client, candidate["url"], target, profile, host, fallback_lock)
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
                        opencli_extract,
                        candidate["url"],
                        profile,
                        f"earnings-pathfinder-{output.name}",
                        host=host,
                    )
                text = normalize_text(raw)
            problems = transcript_quality_problems(text)
            if problems:
                raise RuntimeError("post-processing quality gate failed: " + "; ".join(problems))
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
                            host=host,
                        )
                    browser_text = normalize_text(browser_raw)
                    if len(browser_text) > len(text) + 1_000:
                        browser_problems = transcript_quality_problems(browser_text)
                        if browser_problems:
                            raise RuntimeError(
                                "browser post-processing quality gate failed: "
                                + "; ".join(browser_problems)
                            )
                        text, fetch_method = browser_text, browser_method
                        verdict = await validate(f"{company} ({ticker})", year, quarter, text)
                except Exception as browser_error:  # noqa: BLE001 - retain the usable primary extraction
                    # Preserve the primary extraction and its partial/rejected
                    # verdict when the browser fallback itself is unavailable.
                    verdict["browser_fallback_error"] = (
                        f"{type(browser_error).__name__}: {browser_error}"[-1_000:]
                    )
            metadata = {"ticker": ticker, "company_name": company, "year": year, "quarter": quarter, "accepted_url_id": candidate["id"], "source_url": candidate["url"], "source_kind": kind(candidate["url"]), "accepted_content_kind": candidate["content_kind"], "accepted_confidence": candidate["confidence"], "fetch_method": fetch_method, "text_chars": len(text), "validation_prompt_version": VALIDATION_PROMPT_VERSION, "validation": verdict}
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
            transcript = await asyncio.to_thread(
                finalize_v2_bundle,
                target,
                metadata,
                text,
                int(candidate["id"]),
            )
            status = "validated" if pathfinder_full and float(verdict.get("confidence", 0)) >= .80 else "rejected"
            if status == "validated":
                cosmos_path = None
                if publisher is not None:
                    publish_result = await asyncio.to_thread(
                        publisher.publish,
                        target,
                        EarningsCallArtifactKey(
                            year, quarter, ticker, int(candidate["id"])
                        ),
                    )
                    # This field is committed only after the Publisher has
                    # remotely verified the immutable version and atomically
                    # replaced `current`.
                    cosmos_path = publish_result.current_path
                db.execute("UPDATE pathfinder_transcripts SET status='rejected', updated_at=? WHERE company_id=? AND accepted_url_id<>? AND status='validated'", (now(), company_id, candidate["id"]))
                db.execute("INSERT OR REPLACE INTO pathfinder_transcripts(company_id, accepted_url_id, candidate_rank, status, source_url, source_kind, local_path, cosmos_path, text_chars, validation_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (company_id, candidate["id"], rank, status, candidate["url"], kind(candidate["url"]), str(transcript), cosmos_path, len(text), json.dumps(verdict), now(), now()))
                db.execute("INSERT OR REPLACE INTO pathfinder_company_status(company_id, status, updated_at) VALUES (?, 'validated', ?)", (company_id, now()))
                print(f"VALIDATED {ticker} rank={rank} chars={len(text)}", flush=True)
                return
            db.execute("INSERT OR REPLACE INTO pathfinder_transcripts(company_id, accepted_url_id, candidate_rank, status, source_url, source_kind, local_path, cosmos_path, text_chars, validation_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)", (company_id, candidate["id"], rank, status, candidate["url"], kind(candidate["url"]), str(transcript), len(text), json.dumps(verdict), now(), now()))
        except Exception as exc:  # noqa: BLE001 - candidate boundary records structured failure
            retryable_publication_failure = isinstance(exc, PublishError)
            try:
                compress_artifact_directory(target)
            except Exception as compression_exc:  # noqa: BLE001 - retain both candidate and storage errors
                exc = RuntimeError(f"{exc}; artifact compression failed: {compression_exc}")
            db.execute("INSERT OR REPLACE INTO pathfinder_transcripts(company_id, accepted_url_id, candidate_rank, status, source_url, source_kind, local_path, cosmos_path, text_chars, validation_json, created_at, updated_at) VALUES (?, ?, ?, 'download_failed', ?, ?, NULL, NULL, NULL, ?, ?, ?)", (company_id, candidate["id"], rank, candidate["url"], kind(candidate["url"]), json.dumps({"error": f"{type(exc).__name__}: {exc}"}), now(), now()))
            if retryable_publication_failure:
                # A Cosmos outage is infrastructure failure, not evidence that
                # every candidate URL is exhausted.  Leave the company without
                # a terminal status so the normal pending selector can retry it.
                print(
                    f"RETRYABLE_PUBLISH_FAILURE {ticker}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                return
    db.execute("INSERT OR REPLACE INTO pathfinder_company_status(company_id, status, updated_at) VALUES (?, 'exhausted', ?)", (company_id, now()))
    print(f"NO_VALID_TRANSCRIPT {ticker}", flush=True)


async def main_async(args) -> None:
    db = db_connect(args.db)
    publisher = None
    if not args.skip_cosmos:
        publisher = EarningsCallPublisher(
            CosmosPublisherConfig(host=args.cosmos_host, root=args.cosmos_root)
        )
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
                await process_company(db, row, args.year, args.quarter, args.output_dir, args.opencli_profile, args.opencli_host, client, fallback_lock, publisher)
        await asyncio.gather(*(guarded(row) for row in rows))
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--year", type=int, default=2026); parser.add_argument("--quarter", default="Q1"); parser.add_argument("--workers", type=int, default=4); parser.add_argument("--limit", type=int, default=20); parser.add_argument("--tickers")
    parser.add_argument("--opencli-host", default=os.getenv("VALUECHAIN_OPENCLI_HOST", "macmini-m4"))
    parser.add_argument("--opencli-profile", default=os.getenv("VALUECHAIN_OPENCLI_PROFILE", "auto-single"))
    parser.add_argument("--cosmos-host", default=os.getenv("VALUECHAIN_COSMOS_HOST", "pi@100.102.250.107"))
    parser.add_argument("--cosmos-root", default=os.getenv("VALUECHAIN_COSMOS_ROOT", "/mnt/hdd8tb/valuechain/earnings_calls"))
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
