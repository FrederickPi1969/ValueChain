"""Paired Google-vs-DuckDuckGo earnings-call search study.

The study is deliberately isolated from the production earnings-call queues.
Each company has two independent search arms, both receive the same four query
strings, and an arm stops only after the normal downloader and strict
Pathfinder validator produce a complete, exact-period call.  Search-result
classification alone is never considered success.

The SQLite database and compressed artifact tree are resumable.  Re-running a
completed study is read-only unless ``--retry-errors`` is supplied.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

# The production workers are executable scripts rather than an installed
# package.  Make their directory importable both when this file is executed
# directly and when a unit test imports it as ``scripts.<module>``.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_earnings_call_downstream import (
    ARTIFACT_SCHEMA_VERSION,
    Job,
    fetch_candidate,
    kind,
    verify_v2_artifact,
    write_bundle_manifest,
    zstd_is_valid,
)
from run_earnings_call_pathfinder import (
    blocked_downloader_host,
    conflicting_source_period,
)
from run_earnings_call_pathfinder import (
    validate as validate_transcript,
)

from valuechain.earnings_call_artifacts import compress_artifact_directory
from valuechain.earnings_call_content import (
    transcript_is_complete,
    transcript_quality_metrics,
)
from valuechain.earnings_calls import (
    USER_AGENT,
    Candidate,
    Judgement,
    Settings,
    eligible,
    judge_candidates_async,
)
from valuechain.remote_opencli import RemoteOpenCLIConfig, RemoteOpenCLIExtractor

GOOGLE_SERP_URL = "http://100.114.26.88:10087/search"
DDG_SERP_URL = "https://serp.frederickpi.com"
STUDY_SEED = "valuechain-search-ab-v1"
ENGINES = ("google", "duckduckgo")
QUERY_VERSION = "conference-youtube-transcript-results-v1"

# Current Q1-2026 population strata.  Equal-size study strata are weighted
# back to these counts in the report.
POPULATION_COUNTS = {
    ("top", "validated"): 52,
    ("top", "exhausted"): 598,
    ("top", "no_accepted"): 350,
    ("next", "validated"): 372,
    ("next", "exhausted"): 438,
    ("next", "no_accepted"): 190,
}

FIXED_SAMPLE = {
    ("top", "validated"): ["PSX", "COP", "FCX", "CCOI", "ABNB", "META", "NWL", "BIDU", "QGEN", "BLDR"],
    ("top", "exhausted"): ["CSCO", "KAI", "PM", "KFY", "WTS", "DAR", "TT", "MDT", "HD", "DBD"],
    ("top", "no_accepted"): ["LFUS", "TDC", "PCTY", "J", "LHX", "TGT", "MAR", "GLW", "MAS", "DRS"],
    ("next", "validated"): ["FE", "SSRM", "NVO", "RSI", "SSB", "NOMD", "LBRT", "RYN", "NGVC", "SLF"],
    ("next", "exhausted"): ["BNY", "CTRE", "VSTS", "RDY", "CINF", "ADNT", "EGP", "DEA", "MTB", "SCCO"],
    ("next", "no_accepted"): ["SPXSF", "PZZA", "ATDRF", "HKHHY", "JHX", "MARUF", "BTU", "BUSE", "TLK", "CAJPY"],
}

SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS experiment_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sample_companies (
  id INTEGER PRIMARY KEY,
  tier TEXT NOT NULL,
  stratum TEXT NOT NULL,
  source_db TEXT NOT NULL,
  source_company_id INTEGER NOT NULL,
  ticker TEXT NOT NULL,
  company_name TEXT NOT NULL,
  sector TEXT NOT NULL,
  population_n INTEGER NOT NULL,
  UNIQUE(tier, ticker)
);
CREATE TABLE IF NOT EXISTS arms (
  company_id INTEGER NOT NULL REFERENCES sample_companies(id),
  engine TEXT NOT NULL CHECK(engine IN ('google','duckduckgo')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','running','completed','failed')),
  success INTEGER NOT NULL DEFAULT 0,
  success_query_ordinal INTEGER,
  successful_candidate_id INTEGER,
  query_count INTEGER NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT,
  error TEXT,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(company_id, engine)
);
CREATE INDEX IF NOT EXISTS ix_study_arms_status ON arms(status, company_id, engine);
CREATE TABLE IF NOT EXISTS search_queries (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES sample_companies(id),
  engine TEXT NOT NULL CHECK(engine IN ('google','duckduckgo')),
  ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 4),
  query TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','running','completed','failed')),
  result_count INTEGER NOT NULL DEFAULT 0,
  latency_ms REAL,
  backend_request_id TEXT,
  error TEXT,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(company_id, engine, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_study_queries_arm
  ON search_queries(company_id, engine, ordinal);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY,
  search_query_id INTEGER NOT NULL REFERENCES search_queries(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL,
  url TEXT NOT NULL,
  normalized_url TEXT NOT NULL,
  title TEXT NOT NULL,
  snippet TEXT NOT NULL,
  source_type TEXT NOT NULL,
  UNIQUE(search_query_id, rank)
);
CREATE INDEX IF NOT EXISTS ix_study_candidates_url ON candidates(normalized_url);
CREATE TABLE IF NOT EXISTS judgements (
  candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('completed','failed')),
  is_target INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0,
  content_kind TEXT NOT NULL DEFAULT 'other',
  reason TEXT NOT NULL DEFAULT '',
  latency_ms REAL,
  error TEXT,
  judged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trials (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL UNIQUE REFERENCES candidates(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','running','validated','rejected','download_failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  latency_ms REAL,
  fetch_method TEXT,
  text_chars INTEGER,
  artifact_path TEXT,
  validation_json TEXT,
  error TEXT,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_study_trials_status ON trials(status, candidate_id);
"""


@dataclass(frozen=True)
class StudyCompany:
    tier: str
    stratum: str
    source_db: str
    source_company_id: int
    ticker: str
    company_name: str
    sector: str
    population_n: int


@dataclass(frozen=True)
class SearchResponse:
    candidates: list[Candidate]
    request_id: str | None = None


@dataclass(frozen=True)
class TrialOutcome:
    status: str
    fetch_method: str | None = None
    text_chars: int | None = None
    artifact_path: str | None = None
    validation: dict | None = None
    error: str | None = None


class SearchBackend(Protocol):
    name: str

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse: ...


JudgeFunction = Callable[
    [str, int, str, list[Candidate]], Awaitable[list[Judgement]]
]
TrialFunction = Callable[
    [sqlite3.Row, sqlite3.Row, Judgement], Awaitable[TrialOutcome]
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    db.executescript(SCHEMA)
    return db


def query_texts(ticker: str, company_name: str, year: int, quarter: str) -> list[str]:
    """The four logical queries mirrored exactly to both engines."""
    base = f"{ticker} {company_name} {year} {quarter.upper()}"
    return [
        f"{base} earnings conference call",
        f"{base} earnings conference call YouTube",
        f"{base} earnings call transcript",
        f"{base} quarterly results conference call",
    ]


def normalize_url(url: str) -> str:
    """Stable URL identity for overlap reporting without altering fetched URLs."""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if host in {"youtube.com", "m.youtube.com"} and parsed.path == "/watch" and query.get("v"):
        return f"https://youtube.com/watch?v={query['v']}"
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
        if video_id:
            return f"https://youtube.com/watch?v={video_id}"
    tracking = {
        key
        for key in query
        if key.lower().startswith("utm_")
        or key.lower() in {"gclid", "fbclid", "ocid", "ref", "source"}
    }
    clean_query = urlencode(sorted((key, value) for key, value in query.items() if key not in tracking))
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunparse(((parsed.scheme or "https").lower(), host, path, "", clean_query, ""))


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def load_fixed_sample(top_db: Path, next_db: Path) -> list[StudyCompany]:
    databases = {"top": top_db, "next": next_db}
    companies: list[StudyCompany] = []
    for (tier, stratum), tickers in FIXED_SAMPLE.items():
        source = databases[tier]
        source_db = sqlite3.connect(source)
        source_db.row_factory = sqlite3.Row
        try:
            for ticker in tickers:
                rows = source_db.execute(
                    "SELECT id,ticker,company_name,COALESCE(sector,'') AS sector "
                    "FROM companies WHERE upper(ticker)=?",
                    (ticker.upper(),),
                ).fetchall()
                if len(rows) != 1:
                    raise RuntimeError(
                        f"fixed sample ticker {tier}/{ticker} resolved to {len(rows)} rows in {source}"
                    )
                row = rows[0]
                companies.append(
                    StudyCompany(
                        tier=tier,
                        stratum=stratum,
                        source_db=str(source.resolve()),
                        source_company_id=int(row["id"]),
                        ticker=str(row["ticker"]),
                        company_name=str(row["company_name"]),
                        sector=str(row["sector"]),
                        population_n=POPULATION_COUNTS[(tier, stratum)],
                    )
                )
        finally:
            source_db.close()
    if len(companies) != 60:
        raise RuntimeError(f"fixed study must contain 60 companies, found {len(companies)}")
    return companies


def initialize_study(
    db_path: Path,
    companies: Sequence[StudyCompany],
    *,
    year: int,
    quarter: str,
    seed: str,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = connect(db_path)
    try:
        sample_payload = [asdict(company) for company in companies]
        sample_hash = hashlib.sha256(
            json.dumps(sample_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        metadata = {
            "year": str(year),
            "quarter": quarter.upper(),
            "seed": seed,
            "query_version": QUERY_VERSION,
            "sample_sha256": sample_hash,
            "git_commit_at_creation": _git_commit(),
        }
        existing = {
            row["key"]: row["value"]
            for row in db.execute("SELECT key,value FROM experiment_meta")
        }
        for key in ("year", "quarter", "seed", "query_version", "sample_sha256"):
            if key in existing and existing[key] != metadata[key]:
                raise RuntimeError(
                    f"study database metadata mismatch for {key}: {existing[key]!r} != {metadata[key]!r}"
                )
        for key, value in metadata.items():
            db.execute(
                "INSERT OR IGNORE INTO experiment_meta(key,value) VALUES (?,?)",
                (key, value),
            )
        stamp = utc_now()
        for company in companies:
            db.execute(
                "INSERT OR IGNORE INTO sample_companies "
                "(tier,stratum,source_db,source_company_id,ticker,company_name,sector,population_n) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    company.tier,
                    company.stratum,
                    company.source_db,
                    company.source_company_id,
                    company.ticker,
                    company.company_name,
                    company.sector,
                    company.population_n,
                ),
            )
            company_id = db.execute(
                "SELECT id FROM sample_companies WHERE tier=? AND ticker=?",
                (company.tier, company.ticker),
            ).fetchone()[0]
            for engine in ENGINES:
                db.execute(
                    "INSERT OR IGNORE INTO arms(company_id,engine,updated_at) VALUES (?,?,?)",
                    (company_id, engine, stamp),
                )
    finally:
        db.close()


def recover_interrupted(db: sqlite3.Connection, *, retry_errors: bool) -> dict[str, int]:
    stamp = utc_now()
    query_running = db.execute(
        "UPDATE search_queries SET status='pending',error='interrupted before query commit',"
        "finished_at=NULL WHERE status='running'"
    ).rowcount
    trial_running = db.execute(
        "UPDATE trials SET status='pending',error='interrupted before trial commit',"
        "finished_at=NULL,updated_at=? WHERE status='running'",
        (stamp,),
    ).rowcount
    arm_running = db.execute(
        "UPDATE arms SET status='pending',worker_id=NULL,error='interrupted worker recovered',"
        "finished_at=NULL,updated_at=? WHERE status='running'",
        (stamp,),
    ).rowcount
    retried_queries = retried_trials = retried_arms = 0
    if retry_errors:
        retried_queries = db.execute(
            "UPDATE search_queries SET status='pending',error=NULL,started_at=NULL,finished_at=NULL "
            "WHERE status='failed'"
        ).rowcount
        retried_trials = db.execute(
            "UPDATE trials SET status='pending',error=NULL,started_at=NULL,finished_at=NULL,"
            "updated_at=? WHERE status='download_failed'",
            (stamp,),
        ).rowcount
        retried_arms = db.execute(
            "UPDATE arms SET status='pending',success=0,success_query_ordinal=NULL,"
            "successful_candidate_id=NULL,error=NULL,worker_id=NULL,finished_at=NULL,updated_at=? "
            "WHERE status='failed' OR (status='completed' AND success=0 AND ("
            "EXISTS (SELECT 1 FROM search_queries q WHERE q.company_id=arms.company_id "
            "AND q.engine=arms.engine AND q.status='pending') OR EXISTS ("
            "SELECT 1 FROM trials t JOIN candidates c ON c.id=t.candidate_id "
            "JOIN search_queries q ON q.id=c.search_query_id WHERE q.company_id=arms.company_id "
            "AND q.engine=arms.engine AND t.status='pending'))) ",
            (stamp,),
        ).rowcount
    return {
        "recovered_arms": arm_running,
        "recovered_queries": query_running,
        "recovered_trials": trial_running,
        "retried_arms": retried_arms,
        "retried_queries": retried_queries,
        "retried_trials": retried_trials,
    }


class GoogleBackend:
    name = "google"

    def __init__(self, client: httpx.AsyncClient, base_url: str = GOOGLE_SERP_URL):
        self.client = client
        self.base_url = base_url

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        # The Tailscale hop is intentionally not proxied.  The Endeavor Google
        # service applies its rotating proxy to the upstream Google request.
        response = await self.client.get(
            self.base_url,
            params={"q": query, "num": min(10, limit)},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        candidates = [
            Candidate(
                url=str(item["link"]),
                title=str(item.get("title", "")),
                snippet=str(item.get("snippet", "")),
                engine=self.name,
                query=query,
            )
            for item in payload.get("results", [])
            if isinstance(item, dict) and item.get("link")
        ][:limit]
        request_id = response.headers.get("x-request-id")
        return SearchResponse(candidates, request_id)


class DuckDuckGoBackend:
    name = "duckduckgo"

    def __init__(self, client: httpx.AsyncClient, base_url: str = DDG_SERP_URL):
        self.client = client
        self.base_url = base_url.rstrip("/")

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        submitted = await self.client.post(
            f"{self.base_url}/search",
            json={"query": query, "type": "web", "pages": 1},
            timeout=30,
        )
        submitted.raise_for_status()
        task_id = str(submitted.json().get("task_id") or "")
        if not task_id:
            raise RuntimeError("DDG service did not return task_id")
        for _ in range(75):
            await asyncio.sleep(1)
            status_response = await self.client.get(
                f"{self.base_url}/search/{task_id}/status", timeout=20
            )
            status_response.raise_for_status()
            status = status_response.json()
            if status.get("status") == "FAILURE":
                raise RuntimeError(str(status.get("error") or status))
            if status.get("status") != "SUCCESS":
                continue
            result_response = await self.client.get(
                f"{self.base_url}/search/{task_id}/result", timeout=30
            )
            result_response.raise_for_status()
            raw = result_response.json().get("results", {})
            records = list(raw.values()) if isinstance(raw, dict) else list(raw or [])
            candidates = [
                Candidate(
                    url=str(item.get("url") or item.get("link") or ""),
                    title=str(item.get("title") or ""),
                    snippet=str(
                        item.get("snippet")
                        or item.get("summary")
                        or item.get("text")
                        or ""
                    ),
                    engine=self.name,
                    query=query,
                )
                for item in records
                if isinstance(item, dict) and (item.get("url") or item.get("link"))
            ][:limit]
            return SearchResponse(candidates, task_id)
        raise TimeoutError("DDG service polling timed out")


async def default_judge(
    company: str,
    year: int,
    quarter: str,
    candidates: list[Candidate],
) -> list[Judgement]:
    return await judge_candidates_async(company, year, quarter, candidates, Settings())


class DownloaderTrialRunner:
    """Use the production downloader, strict validator, and zstd storage."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        artifact_root: Path,
        year: int,
        quarter: str,
        extractor: RemoteOpenCLIExtractor | None,
        fallback_lock: asyncio.Lock,
    ) -> None:
        self.client = client
        self.artifact_root = artifact_root
        self.year = year
        self.quarter = quarter.upper()
        self.extractor = extractor
        self.fallback_lock = fallback_lock

    async def __call__(
        self,
        company: sqlite3.Row,
        candidate: sqlite3.Row,
        judgement: Judgement,
    ) -> TrialOutcome:
        url = str(candidate["url"])
        title = str(candidate["title"])
        source_kind = kind(url)
        if blocked_downloader_host(url):
            return TrialOutcome(
                "download_failed",
                error="source domain is blacklisted after repeated unreadable/paywall downloads",
            )
        if conflicting_source_period(title, url, self.year, self.quarter):
            return TrialOutcome(
                "rejected",
                error="source URL/title explicitly names a different target period",
            )

        target = (
            self.artifact_root
            / str(company["ticker"])
            / str(candidate["engine"])
            / f"q{candidate['query_ordinal']}"
            / str(candidate["id"])
        )
        recovered = self._recover_artifact(target, source_kind, judgement.content_kind)
        if recovered is not None:
            return recovered

        staging_root = self.artifact_root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=staging_root,
            prefix=f"{company['ticker']}-{candidate['engine']}-{candidate['id']}-",
        ) as temporary:
            staging = Path(temporary)
            job = Job(
                accepted_url_id=int(candidate["id"]),
                url=url,
                source_kind=source_kind,
                attempt_no=1,
                worker_id="search-ab",
            )
            text, method, quality, primary_error = await fetch_candidate(
                self.client,
                job,
                staging,
                self.extractor,
                self.fallback_lock,
                force_opencli=False,
            )
            if len(text) < 2_500:
                raise RuntimeError(f"cleaned transcript too short: {len(text)}")
            company_target = f"{company['company_name']} ({company['ticker']})"
            verdict = await validate_transcript(
                company_target, self.year, self.quarter, text
            )
            complete = transcript_is_complete(
                verdict,
                text,
                source_kind=source_kind,
                content_kind=judgement.content_kind,
            )
            # A substantial ULSCAR excerpt can still be truncated.  Mirror the
            # repaired Pathfinder behavior and give call-like web text one
            # remote, paginated OpenCLI attempt before rejecting it.
            if (
                source_kind == "web"
                and not complete
                and not method.startswith("opencli.")
                and verdict.get("document_type") in {"earnings_call", "partial_call"}
                and self.extractor is not None
            ):
                try:
                    browser_text, browser_method, browser_quality, browser_error = (
                        await fetch_candidate(
                            self.client,
                            job,
                            staging,
                            self.extractor,
                            self.fallback_lock,
                            force_opencli=True,
                            opencli_reason=(
                                "strict transcript gate rejected ULSCAR extraction"
                            ),
                        )
                    )
                    if len(browser_text) > len(text) + 1_000:
                        text = browser_text
                        method = browser_method
                        quality = browser_quality
                        primary_error = browser_error or primary_error
                        verdict = await validate_transcript(
                            company_target, self.year, self.quarter, text
                        )
                        complete = transcript_is_complete(
                            verdict,
                            text,
                            source_kind=source_kind,
                            content_kind=judgement.content_kind,
                        )
                except Exception as exc:  # noqa: BLE001 - optional fallback is best effort
                    primary_error = (
                        f"{primary_error or ''}; remote OpenCLI retry: "
                        f"{type(exc).__name__}: {exc}"
                    ).strip("; ")

            artifact = await asyncio.to_thread(
                self._persist_artifact,
                staging,
                target,
                company,
                candidate,
                judgement,
                text,
                method,
                quality,
                primary_error,
                verdict,
                complete,
            )
            return TrialOutcome(
                "validated" if complete else "rejected",
                fetch_method=method,
                text_chars=len(text),
                artifact_path=str(artifact),
                validation=verdict,
            )

    def _persist_artifact(
        self,
        staging: Path,
        target: Path,
        company: sqlite3.Row,
        candidate: sqlite3.Row,
        judgement: Judgement,
        text: str,
        method: str,
        quality: dict,
        primary_error: str | None,
        verdict: dict,
        complete: bool,
    ) -> Path:
        text_bytes = text.encode("utf-8")
        digest = hashlib.sha256(text_bytes).hexdigest()
        transcript = staging / "transcript.txt"
        transcript.write_bytes(text_bytes)
        metadata = {
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "study": "google-vs-duckduckgo-earnings-search",
            "ticker": company["ticker"],
            "company_name": company["company_name"],
            "year": self.year,
            "quarter": self.quarter,
            "engine": candidate["engine"],
            "query_ordinal": candidate["query_ordinal"],
            "candidate_id": candidate["id"],
            "accepted_url_id": candidate["id"],
            "source_url": candidate["url"],
            "source_kind": kind(candidate["url"]),
            "content_kind": judgement.content_kind,
            "fetch_method": method,
            "fetched_at": utc_now(),
            "text_chars": len(text),
            "text_sha256": digest,
            "quality": quality or transcript_quality_metrics(text),
            "primary_error": primary_error,
            "validation": verdict,
            "strict_complete": complete,
        }
        (staging / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        compressed = compress_artifact_directory(staging)
        write_bundle_manifest(staging, int(candidate["id"]))
        compress_artifact_directory(staging)
        for path in staging.iterdir():
            if path.is_file() and path.suffix == ".zst" and not zstd_is_valid(path):
                raise RuntimeError(f"zstd integrity check failed: {path.name}")
        target.mkdir(parents=True, exist_ok=True)
        for path in staging.iterdir():
            if path.is_file():
                path.replace(target / path.name)
        artifact = target / compressed[transcript].name
        verified = verify_v2_artifact(target)
        if verified is None:
            raise RuntimeError("promoted study artifact failed manifest/hash/zstd audit")
        return artifact

    def _recover_artifact(
        self, target: Path, source_kind: str, content_kind: str
    ) -> TrialOutcome | None:
        verified = verify_v2_artifact(target)
        if verified is None:
            return None
        transcript_path, metadata = verified
        verdict = metadata.get("validation")
        if not isinstance(verdict, dict):
            return None
        text = _read_zstd_text(transcript_path)
        complete = transcript_is_complete(
            verdict,
            text,
            source_kind=source_kind,
            content_kind=content_kind,
        )
        return TrialOutcome(
            "validated" if complete else "rejected",
            fetch_method=str(metadata.get("fetch_method") or "artifact.recovered"),
            text_chars=len(text),
            artifact_path=str(transcript_path),
            validation=verdict,
        )


def _read_zstd_text(path: Path) -> str:
    executable = shutil.which("zstd")
    if not executable:
        raise RuntimeError("zstd is unavailable")
    completed = subprocess.run(
        [executable, "-q", "-d", "-c", str(path)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return completed.stdout.decode("utf-8")


def claim_arm(db: sqlite3.Connection, worker_id: str) -> sqlite3.Row | None:
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT a.*,c.tier,c.stratum,c.ticker,c.company_name,c.sector,c.population_n "
            "FROM arms a JOIN sample_companies c ON c.id=a.company_id "
            "WHERE a.status='pending' ORDER BY c.id, CASE a.engine WHEN 'google' THEN 0 ELSE 1 END "
            "LIMIT 1"
        ).fetchone()
        if row is not None:
            stamp = utc_now()
            db.execute(
                "UPDATE arms SET status='running',worker_id=?,attempt_count=attempt_count+1,"
                "started_at=COALESCE(started_at,?),updated_at=? "
                "WHERE company_id=? AND engine=? AND status='pending'",
                (worker_id, stamp, stamp, row["company_id"], row["engine"]),
            )
        db.execute("COMMIT")
        return row
    except Exception:
        db.execute("ROLLBACK")
        raise


async def ensure_search_query(
    db: sqlite3.Connection,
    company: sqlite3.Row,
    backend: SearchBackend,
    ordinal: int,
    query: str,
) -> list[sqlite3.Row]:
    db.execute(
        "INSERT OR IGNORE INTO search_queries(company_id,engine,ordinal,query,status) "
        "VALUES (?,?,?,?,'pending')",
        (company["company_id"], backend.name, ordinal, query),
    )
    row = db.execute(
        "SELECT * FROM search_queries WHERE company_id=? AND engine=? AND ordinal=?",
        (company["company_id"], backend.name, ordinal),
    ).fetchone()
    if row["query"] != query:
        raise RuntimeError("stored query differs from fixed query contract")
    if row["status"] in {"completed", "failed"}:
        return db.execute(
            "SELECT c.*,? AS engine,? AS query_ordinal FROM candidates c "
            "WHERE c.search_query_id=? ORDER BY c.rank",
            (backend.name, ordinal, row["id"]),
        ).fetchall()

    started = utc_now()
    db.execute(
        "UPDATE search_queries SET status='running',started_at=?,finished_at=NULL,error=NULL "
        "WHERE id=?",
        (started, row["id"]),
    )
    clock = time.perf_counter()
    try:
        response = await backend.search(query, limit=10)
        latency_ms = round((time.perf_counter() - clock) * 1000, 3)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM candidates WHERE search_query_id=?", (row["id"],))
            for rank, candidate in enumerate(response.candidates, 1):
                db.execute(
                    "INSERT INTO candidates(search_query_id,rank,url,normalized_url,title,snippet,source_type) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        rank,
                        candidate.url,
                        normalize_url(candidate.url),
                        candidate.title,
                        candidate.snippet,
                        candidate.source_type,
                    ),
                )
            db.execute(
                "UPDATE search_queries SET status='completed',result_count=?,latency_ms=?,"
                "backend_request_id=?,error=NULL,finished_at=? WHERE id=?",
                (
                    len(response.candidates),
                    latency_ms,
                    response.request_id,
                    utc_now(),
                    row["id"],
                ),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    except Exception as exc:  # noqa: BLE001 - persist backend transport failures
        latency_ms = round((time.perf_counter() - clock) * 1000, 3)
        db.execute(
            "UPDATE search_queries SET status='failed',result_count=0,latency_ms=?,error=?,"
            "finished_at=? WHERE id=?",
            (latency_ms, f"{type(exc).__name__}: {exc}"[-4000:], utc_now(), row["id"]),
        )
    return db.execute(
        "SELECT c.*,? AS engine,? AS query_ordinal FROM candidates c "
        "WHERE c.search_query_id=? ORDER BY c.rank",
        (backend.name, ordinal, row["id"]),
    ).fetchall()


async def ensure_judgements(
    db: sqlite3.Connection,
    company: sqlite3.Row,
    candidates: list[sqlite3.Row],
    *,
    year: int,
    quarter: str,
    judge_fn: JudgeFunction,
) -> list[tuple[sqlite3.Row, Judgement]]:
    if not candidates:
        return []
    existing = {
        row["candidate_id"]: row
        for row in db.execute(
            f"SELECT * FROM judgements WHERE candidate_id IN ({','.join('?' for _ in candidates)})",
            [row["id"] for row in candidates],
        )
    }
    if len(existing) == len(candidates) and all(
        row["status"] == "completed" for row in existing.values()
    ):
        return [
            (
                candidate,
                Judgement(
                    index,
                    bool(existing[candidate["id"]]["is_target"]),
                    float(existing[candidate["id"]]["confidence"]),
                    str(existing[candidate["id"]]["content_kind"]),
                    str(existing[candidate["id"]]["reason"]),
                ),
            )
            for index, candidate in enumerate(candidates)
        ]

    model_candidates = [
        Candidate(
            url=str(row["url"]),
            title=str(row["title"]),
            snippet=str(row["snippet"]),
            engine=str(row["engine"]),
            query="",
            source_type=str(row["source_type"]),
        )
        for row in candidates
    ]
    target = f"{company['company_name']} ({company['ticker']})"
    clock = time.perf_counter()
    try:
        verdicts = await judge_fn(target, year, quarter, model_candidates)
        latency_ms = round((time.perf_counter() - clock) * 1000, 3)
    except Exception as exc:
        latency_ms = round((time.perf_counter() - clock) * 1000, 3)
        error = f"{type(exc).__name__}: {exc}"[-4000:]
        for candidate in candidates:
            db.execute(
                "INSERT OR REPLACE INTO judgements(candidate_id,status,latency_ms,error,judged_at) "
                "VALUES (?,'failed',?,?,?)",
                (candidate["id"], latency_ms, error, utc_now()),
            )
        raise
    by_index = {verdict.candidate_index: verdict for verdict in verdicts}
    output: list[tuple[sqlite3.Row, Judgement]] = []
    omitted: list[int] = []
    for index, candidate in enumerate(candidates):
        verdict = by_index.get(index)
        if verdict is None:
            omitted.append(index)
            db.execute(
                "INSERT OR REPLACE INTO judgements(candidate_id,status,latency_ms,error,judged_at) "
                "VALUES (?,'failed',?,'Qwen omitted candidate from response',?)",
                (candidate["id"], latency_ms, utc_now()),
            )
            continue
        db.execute(
            "INSERT OR REPLACE INTO judgements(candidate_id,status,is_target,confidence,"
            "content_kind,reason,latency_ms,error,judged_at) "
            "VALUES (?,'completed',?,?,?,?,?,NULL,?)",
            (
                candidate["id"],
                int(verdict.is_target),
                verdict.confidence,
                verdict.content_kind,
                verdict.reason,
                latency_ms,
                utc_now(),
            ),
        )
        output.append((candidate, verdict))
    if omitted:
        raise RuntimeError(f"Qwen omitted candidate indexes: {omitted}")
    return output


def _candidate_for_eligibility(row: sqlite3.Row) -> Candidate:
    return Candidate(
        url=str(row["url"]),
        title=str(row["title"]),
        snippet=str(row["snippet"]),
        engine=str(row["engine"]),
        query="",
        source_type=str(row["source_type"]),
    )


async def ensure_trial(
    db: sqlite3.Connection,
    company: sqlite3.Row,
    candidate: sqlite3.Row,
    judgement: Judgement,
    trial_fn: TrialFunction,
) -> TrialOutcome:
    stamp = utc_now()
    db.execute(
        "INSERT OR IGNORE INTO trials(candidate_id,status,updated_at) VALUES (?,'pending',?)",
        (candidate["id"], stamp),
    )
    row = db.execute(
        "SELECT * FROM trials WHERE candidate_id=?", (candidate["id"],)
    ).fetchone()
    if row["status"] in {"validated", "rejected", "download_failed"}:
        return TrialOutcome(
            status=str(row["status"]),
            fetch_method=row["fetch_method"],
            text_chars=row["text_chars"],
            artifact_path=row["artifact_path"],
            validation=json.loads(row["validation_json"]) if row["validation_json"] else None,
            error=row["error"],
        )
    db.execute(
        "UPDATE trials SET status='running',attempt_count=attempt_count+1,started_at=?,"
        "finished_at=NULL,error=NULL,updated_at=? WHERE candidate_id=?",
        (stamp, stamp, candidate["id"]),
    )
    clock = time.perf_counter()
    try:
        outcome = await trial_fn(company, candidate, judgement)
        if outcome.status not in {"validated", "rejected", "download_failed"}:
            raise ValueError(f"invalid trial outcome status {outcome.status!r}")
    except Exception as exc:  # noqa: BLE001 - candidate boundary records every failure
        outcome = TrialOutcome(
            "download_failed", error=f"{type(exc).__name__}: {exc}"[-4000:]
        )
    latency_ms = round((time.perf_counter() - clock) * 1000, 3)
    db.execute(
        "UPDATE trials SET status=?,latency_ms=?,fetch_method=?,text_chars=?,artifact_path=?,"
        "validation_json=?,error=?,finished_at=?,updated_at=? WHERE candidate_id=?",
        (
            outcome.status,
            latency_ms,
            outcome.fetch_method,
            outcome.text_chars,
            outcome.artifact_path,
            json.dumps(outcome.validation, ensure_ascii=False) if outcome.validation else None,
            outcome.error,
            utc_now(),
            utc_now(),
            candidate["id"],
        ),
    )
    return outcome


async def process_arm(
    db: sqlite3.Connection,
    company: sqlite3.Row,
    backend: SearchBackend,
    trial_fn: TrialFunction,
    judge_fn: JudgeFunction,
    *,
    year: int,
    quarter: str,
) -> None:
    success_candidate: int | None = None
    success_ordinal: int | None = None
    queries = query_texts(company["ticker"], company["company_name"], year, quarter)
    try:
        for ordinal, query in enumerate(queries, 1):
            candidates = await ensure_search_query(
                db, company, backend, ordinal, query
            )
            judged = await ensure_judgements(
                db,
                company,
                candidates,
                year=year,
                quarter=quarter,
                judge_fn=judge_fn,
            )
            priority = {
                "official_transcript": 0,
                "third_party_transcript": 1,
                "official_webcast": 2,
                "youtube_video": 3,
                "other": 4,
            }
            judged.sort(
                key=lambda pair: (
                    priority.get(pair[1].content_kind, 4),
                    -pair[1].confidence,
                    pair[0]["rank"],
                )
            )
            for candidate, judgement in judged:
                if not eligible(_candidate_for_eligibility(candidate), judgement):
                    continue
                outcome = await ensure_trial(
                    db, company, candidate, judgement, trial_fn
                )
                if outcome.status == "validated":
                    success_candidate = int(candidate["id"])
                    success_ordinal = ordinal
                    break
            if success_candidate is not None:
                break
        query_count = db.execute(
            "SELECT COUNT(*) FROM search_queries WHERE company_id=? AND engine=? "
            "AND status IN ('completed','failed')",
            (company["company_id"], backend.name),
        ).fetchone()[0]
        db.execute(
            "UPDATE arms SET status='completed',success=?,success_query_ordinal=?,"
            "successful_candidate_id=?,query_count=?,worker_id=NULL,error=NULL,finished_at=?,"
            "updated_at=? WHERE company_id=? AND engine=?",
            (
                int(success_candidate is not None),
                success_ordinal,
                success_candidate,
                query_count,
                utc_now(),
                utc_now(),
                company["company_id"],
                backend.name,
            ),
        )
        print(
            f"STUDY {company['ticker']} {backend.name} "
            f"success={success_candidate is not None} queries={query_count}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - arm boundary must remain resumable
        query_count = db.execute(
            "SELECT COUNT(*) FROM search_queries WHERE company_id=? AND engine=? "
            "AND status IN ('completed','failed')",
            (company["company_id"], backend.name),
        ).fetchone()[0]
        db.execute(
            "UPDATE arms SET status='failed',query_count=?,worker_id=NULL,error=?,finished_at=?,"
            "updated_at=? WHERE company_id=? AND engine=?",
            (
                query_count,
                f"{type(exc).__name__}: {exc}"[-4000:],
                utc_now(),
                utc_now(),
                company["company_id"],
                backend.name,
            ),
        )
        print(
            f"STUDY_FAILED {company['ticker']} {backend.name}: {type(exc).__name__}: {exc}",
            flush=True,
        )


async def run_workers(
    db_path: Path,
    *,
    backends: dict[str, SearchBackend],
    trial_fn: TrialFunction,
    judge_fn: JudgeFunction = default_judge,
    year: int,
    quarter: str,
    workers: int = 8,
) -> None:
    if set(backends) != set(ENGINES):
        raise ValueError("study requires explicit google and duckduckgo backends")

    async def worker(index: int) -> None:
        db = connect(db_path)
        worker_id = f"{socket.gethostname()}:{os.getpid()}:{index}:{uuid.uuid4().hex[:8]}"
        try:
            while True:
                arm = claim_arm(db, worker_id)
                if arm is None:
                    return
                await process_arm(
                    db,
                    arm,
                    backends[str(arm["engine"])],
                    trial_fn,
                    judge_fn,
                    year=year,
                    quarter=quarter,
                )
        finally:
            db.close()

    await asyncio.gather(*(worker(index) for index in range(workers)))


def exact_mcnemar_p(google_only: int, ddg_only: int) -> float:
    """Two-sided exact McNemar/binomial test without a scipy dependency."""
    discordant = google_only + ddg_only
    if discordant == 0:
        return 1.0
    tail = min(google_only, ddg_only)
    probability = sum(math.comb(discordant, value) for value in range(tail + 1)) / (
        2**discordant
    )
    return min(1.0, 2 * probability)


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _weighted_rate(
    pairs_by_stratum: dict[tuple[str, str], list[dict]], key: str
) -> float | None:
    total_population = sum(POPULATION_COUNTS.values())
    if any(not pairs_by_stratum.get(stratum) for stratum in POPULATION_COUNTS):
        return None
    return sum(
        POPULATION_COUNTS[stratum]
        * sum(float(row[key]) for row in pairs_by_stratum[stratum])
        / len(pairs_by_stratum[stratum])
        for stratum in POPULATION_COUNTS
    ) / total_population


def build_report(
    db_path: Path,
    *,
    bootstrap_replicates: int = 2_000,
    seed: str = STUDY_SEED,
) -> dict:
    db = connect(db_path)
    try:
        arms = db.execute(
            "SELECT a.*,c.tier,c.stratum,c.ticker,c.population_n FROM arms a "
            "JOIN sample_companies c ON c.id=a.company_id ORDER BY c.id,a.engine"
        ).fetchall()
        status = {
            engine: {
                row["status"]: row["n"]
                for row in db.execute(
                    "SELECT status,COUNT(*) n FROM arms WHERE engine=? GROUP BY status",
                    (engine,),
                )
            }
            for engine in ENGINES
        }
        success_at: dict[str, dict[str, dict[str, float | int]]] = {}
        for engine in ENGINES:
            completed = [
                row for row in arms if row["engine"] == engine and row["status"] == "completed"
            ]
            success_at[engine] = {}
            for ordinal in range(1, 5):
                successes = sum(
                    bool(row["success"])
                    and int(row["success_query_ordinal"] or 99) <= ordinal
                    for row in completed
                )
                success_at[engine][f"@{ordinal}"] = {
                    "successes": successes,
                    "denominator": len(completed),
                    "rate": successes / len(completed) if completed else 0.0,
                }

        by_company: dict[int, dict[str, sqlite3.Row]] = {}
        for row in arms:
            by_company.setdefault(int(row["company_id"]), {})[str(row["engine"])] = row
        pairs: list[dict] = []
        for engine_rows in by_company.values():
            if set(engine_rows) != set(ENGINES):
                continue
            google = engine_rows["google"]
            ddg = engine_rows["duckduckgo"]
            if google["status"] != "completed" or ddg["status"] != "completed":
                continue
            pairs.append(
                {
                    "tier": google["tier"],
                    "stratum": google["stratum"],
                    "google": int(google["success"]),
                    "duckduckgo": int(ddg["success"]),
                    "google_queries": int(google["query_count"]),
                    "duckduckgo_queries": int(ddg["query_count"]),
                }
            )
        both = sum(row["google"] and row["duckduckgo"] for row in pairs)
        google_only = sum(row["google"] and not row["duckduckgo"] for row in pairs)
        ddg_only = sum(row["duckduckgo"] and not row["google"] for row in pairs)
        neither = sum(not row["google"] and not row["duckduckgo"] for row in pairs)
        pairs_by_stratum: dict[tuple[str, str], list[dict]] = {
            stratum: [] for stratum in POPULATION_COUNTS
        }
        for row in pairs:
            pairs_by_stratum[(row["tier"], row["stratum"])].append(row)

        weighted_google = _weighted_rate(pairs_by_stratum, "google")
        weighted_ddg = _weighted_rate(pairs_by_stratum, "duckduckgo")
        weighted_union = None
        if weighted_google is not None:
            union_groups = {
                stratum: [
                    {**row, "union": int(row["google"] or row["duckduckgo"])}
                    for row in rows
                ]
                for stratum, rows in pairs_by_stratum.items()
            }
            weighted_union = _weighted_rate(union_groups, "union")

        bootstrap_differences: list[float] = []
        rng = random.Random(seed)
        if weighted_google is not None and bootstrap_replicates > 0:
            for _ in range(bootstrap_replicates):
                sampled = {
                    stratum: [rng.choice(rows) for _ in rows]
                    for stratum, rows in pairs_by_stratum.items()
                }
                google_rate = _weighted_rate(sampled, "google")
                ddg_rate = _weighted_rate(sampled, "duckduckgo")
                if google_rate is not None and ddg_rate is not None:
                    bootstrap_differences.append(ddg_rate - google_rate)

        google_baseline = sum(row["google_queries"] for row in pairs)
        google_after_ddg = sum(
            row["google_queries"] for row in pairs if not row["duckduckgo"]
        )
        raw_savings = (
            1 - google_after_ddg / google_baseline if google_baseline else None
        )
        weighted_baseline = weighted_after = 0.0
        weighted_savings = None
        if weighted_google is not None:
            total_population = sum(POPULATION_COUNTS.values())
            for stratum, rows in pairs_by_stratum.items():
                weight = POPULATION_COUNTS[stratum] / total_population
                weighted_baseline += weight * sum(
                    row["google_queries"] for row in rows
                ) / len(rows)
                weighted_after += weight * sum(
                    row["google_queries"] * (not row["duckduckgo"])
                    for row in rows
                ) / len(rows)
            if weighted_baseline:
                weighted_savings = 1 - weighted_after / weighted_baseline

        query_errors = {
            engine: db.execute(
                "SELECT COUNT(*) FROM search_queries WHERE engine=? AND status='failed'",
                (engine,),
            ).fetchone()[0]
            for engine in ENGINES
        }
        latency = {
            engine: {
                "mean_ms": row["mean_ms"],
                "max_ms": row["max_ms"],
                "requests": row["requests"],
            }
            for engine in ENGINES
            for row in [
                db.execute(
                    "SELECT AVG(latency_ms) mean_ms,MAX(latency_ms) max_ms,COUNT(*) requests "
                    "FROM search_queries WHERE engine=? AND latency_ms IS NOT NULL",
                    (engine,),
                ).fetchone()
            ]
        }
        exact_overlap = db.execute(
            "SELECT COUNT(DISTINCT g.normalized_url) FROM candidates g "
            "JOIN search_queries gq ON gq.id=g.search_query_id AND gq.engine='google' "
            "JOIN candidates d ON d.normalized_url=g.normalized_url "
            "JOIN search_queries dq ON dq.id=d.search_query_id AND dq.engine='duckduckgo' "
            "WHERE gq.company_id=dq.company_id"
        ).fetchone()[0]
        return {
            "arm_status": status,
            "success_at": success_at,
            "completed_pairs": len(pairs),
            "paired_overlap": {
                "both": both,
                "google_only": google_only,
                "duckduckgo_only": ddg_only,
                "neither": neither,
                "union_success": both + google_only + ddg_only,
                "normalized_url_overlap": exact_overlap,
            },
            "weighted_success_rate": {
                "google": weighted_google,
                "duckduckgo": weighted_ddg,
                "union": weighted_union,
                "duckduckgo_minus_google": (
                    weighted_ddg - weighted_google
                    if weighted_google is not None and weighted_ddg is not None
                    else None
                ),
                "provisional": len(pairs) < 60,
            },
            "bootstrap_95pct_ci_duckduckgo_minus_google": [
                _percentile(bootstrap_differences, 0.025),
                _percentile(bootstrap_differences, 0.975),
            ],
            "mcnemar_exact_two_sided_p": exact_mcnemar_p(google_only, ddg_only),
            "ddg_first_google_quota": {
                "google_only_requests_observed": google_baseline,
                "google_requests_after_ddg_first": google_after_ddg,
                "raw_fraction_saved": raw_savings,
                "weighted_fraction_saved": weighted_savings,
            },
            "search_errors": query_errors,
            "search_latency": latency,
        }
    finally:
        db.close()


def print_report(report: dict) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


async def main_async(args: argparse.Namespace) -> None:
    companies = load_fixed_sample(args.top_db, args.next_db)
    initialize_study(
        args.db,
        companies,
        year=args.year,
        quarter=args.quarter,
        seed=args.seed,
    )
    setup = connect(args.db)
    try:
        recovered = recover_interrupted(setup, retry_errors=args.retry_errors)
    finally:
        setup.close()
    print(json.dumps(recovered, sort_keys=True), flush=True)

    limits = httpx.Limits(max_connections=max(24, args.workers * 3), max_keepalive_connections=16)
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, limits=limits, follow_redirects=True
    ) as client:
        extractor = None
        if args.opencli_host and args.opencli_profile:
            extractor = RemoteOpenCLIExtractor(
                RemoteOpenCLIConfig(
                    host=args.opencli_host,
                    profile=args.opencli_profile,
                    executable=args.opencli_executable,
                    helper_executable=args.opencli_helper,
                )
            )
        trial_runner = DownloaderTrialRunner(
            client=client,
            artifact_root=args.artifact_dir,
            year=args.year,
            quarter=args.quarter,
            extractor=extractor,
            fallback_lock=asyncio.Lock(),
        )
        await run_workers(
            args.db,
            backends={
                "google": GoogleBackend(client, args.google_url),
                "duckduckgo": DuckDuckGoBackend(client, args.ddg_url),
            },
            trial_fn=trial_runner,
            year=args.year,
            quarter=args.quarter,
            workers=args.workers,
        )
    print_report(
        build_report(
            args.db,
            bootstrap_replicates=args.bootstrap_replicates,
            seed=args.seed,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument(
        "--top-db",
        type=Path,
        default=Path("data/earnings_calls/sec_top_1000_q1_2026.sqlite3"),
    )
    parser.add_argument(
        "--next-db",
        type=Path,
        default=Path("data/earnings_calls/sec_next_1000_q1_2026.sqlite3"),
    )
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--quarter", default="Q1")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", default=STUDY_SEED)
    parser.add_argument("--google-url", default=GOOGLE_SERP_URL)
    parser.add_argument("--ddg-url", default=DDG_SERP_URL)
    parser.add_argument("--bootstrap-replicates", type=int, default=2_000)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument(
        "--opencli-host", default=os.getenv("VALUECHAIN_OPENCLI_HOST", "macmini-m4")
    )
    parser.add_argument(
        "--opencli-profile", default=os.getenv("VALUECHAIN_OPENCLI_PROFILE", "auto-single")
    )
    parser.add_argument(
        "--opencli-executable",
        default=os.getenv("VALUECHAIN_OPENCLI_EXECUTABLE", "/opt/homebrew/bin/opencli"),
    )
    parser.add_argument(
        "--opencli-helper",
        default=os.getenv(
            "VALUECHAIN_OPENCLI_HELPER",
            "/Users/frederickpi/.local/bin/valuechain-opencli-extract",
        ),
    )
    args = parser.parse_args()
    if args.workers < 1 or args.bootstrap_replicates < 0:
        parser.error("workers must be positive and bootstrap-replicates non-negative")
    if args.quarter.upper() not in {"Q1", "Q2", "Q3", "Q4"}:
        parser.error("quarter must be Q1, Q2, Q3, or Q4")
    if not args.report_only and args.artifact_dir is None:
        parser.error("--artifact-dir is required unless --report-only is used")
    return args


def main() -> None:
    args = parse_args()
    if args.report_only:
        print_report(
            build_report(
                args.db,
                bootstrap_replicates=args.bootstrap_replicates,
                seed=args.seed,
            )
        )
        return
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
