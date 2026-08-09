"""Resumable SQLite batch collector for bounded quarterly earnings-call search."""
from __future__ import annotations

import argparse
import csv
import sqlite3
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from valuechain.earnings_calls import (
    Settings, _rotating_proxy, best_bet_queries, eligible, judge_candidates, search_query,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, company_name TEXT NOT NULL, sector TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed')),
  query_count INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(ticker, company_name)
);
CREATE TABLE IF NOT EXISTS search_queries (
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), ordinal INTEGER NOT NULL,
  query TEXT NOT NULL, engine TEXT NOT NULL, result_count INTEGER NOT NULL DEFAULT 0,
  searched_at TEXT NOT NULL, stopped_early INTEGER NOT NULL DEFAULT 0, error TEXT,
  UNIQUE(company_id, ordinal)
);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY, search_query_id INTEGER NOT NULL REFERENCES search_queries(id), ordinal INTEGER NOT NULL,
  url TEXT NOT NULL, title TEXT NOT NULL, snippet TEXT NOT NULL, source_type TEXT NOT NULL,
  UNIQUE(search_query_id, ordinal)
);
CREATE TABLE IF NOT EXISTS judgements (
  candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id), is_target INTEGER NOT NULL,
  confidence REAL NOT NULL, content_kind TEXT NOT NULL, reason TEXT NOT NULL, judged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accepted_urls (
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  url TEXT NOT NULL, title TEXT NOT NULL, confidence REAL NOT NULL, content_kind TEXT NOT NULL,
  accepted_at TEXT NOT NULL, UNIQUE(company_id, url)
);
CREATE INDEX IF NOT EXISTS ix_companies_status ON companies(status, id);
CREATE INDEX IF NOT EXISTS ix_accepted_urls_company ON accepted_urls(company_id);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(SCHEMA)
    return conn


def initialize(csv_path: Path, db_path: Path) -> int:
    conn = connect(db_path)
    inserted = 0
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker, name = row["Ticker/Abbreviation"].strip(), row["Full Name"].strip()
            cursor = conn.execute("INSERT OR IGNORE INTO companies(ticker, company_name, sector, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (ticker, name, row.get("Sector/Industry", "").strip(), now(), now()))
            inserted += cursor.rowcount
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    conn.close()
    print(f"initialized={inserted} total={total}")
    return total


def claim(conn: sqlite3.Connection) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM companies WHERE status = 'pending' ORDER BY id LIMIT 1").fetchone()
    if row is not None:
        conn.execute("UPDATE companies SET status='running', updated_at=? WHERE id=?", (now(), row["id"]))
    conn.execute("COMMIT")
    return row


def process_one(conn: sqlite3.Connection, company: sqlite3.Row, year: int, quarter: str, high_confidence: float) -> None:
    company_id = company["id"]
    target = f"{company['company_name']} ({company['ticker']})"
    settings = Settings()
    external_proxies = settings.proxies or _rotating_proxy()
    query_count = 0
    try:
        for ordinal, query in enumerate(best_bet_queries(target, year, quarter), 1):
            engine, found = search_query(query, proxies=external_proxies)
            query_count += 1
            cursor = conn.execute("INSERT INTO search_queries(company_id, ordinal, query, engine, result_count, searched_at) VALUES (?, ?, ?, ?, ?, ?)", (company_id, ordinal, query, engine, len(found), now()))
            query_id = cursor.lastrowid
            candidate_ids: list[int] = []
            for candidate_ordinal, candidate in enumerate(found):
                candidate_ids.append(conn.execute("INSERT INTO candidates(search_query_id, ordinal, url, title, snippet, source_type) VALUES (?, ?, ?, ?, ?, ?)", (query_id, candidate_ordinal, candidate.url, candidate.title, candidate.snippet, candidate.source_type)).lastrowid)
            judgements = judge_candidates(target, year, quarter, found, settings)
            early = False
            for judgement in judgements:
                candidate_id = candidate_ids[judgement.candidate_index]
                conn.execute("INSERT INTO judgements(candidate_id, is_target, confidence, content_kind, reason, judged_at) VALUES (?, ?, ?, ?, ?, ?)", (candidate_id, int(judgement.is_target), judgement.confidence, judgement.content_kind, judgement.reason, now()))
                candidate = found[judgement.candidate_index]
                if eligible(candidate, judgement):
                    conn.execute("INSERT OR IGNORE INTO accepted_urls(company_id, candidate_id, url, title, confidence, content_kind, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (company_id, candidate_id, candidate.url, candidate.title, judgement.confidence, judgement.content_kind, now()))
                    early = early or judgement.confidence >= high_confidence
            if early:
                conn.execute("UPDATE search_queries SET stopped_early=1 WHERE id=?", (query_id,))
                break
        conn.execute("UPDATE companies SET status='completed', query_count=?, updated_at=?, error=NULL WHERE id=?", (query_count, now(), company_id))
    except Exception as exc:
        conn.execute("UPDATE companies SET status='failed', query_count=?, updated_at=?, error=? WHERE id=?", (query_count, now(), f"{type(exc).__name__}: {exc}", company_id))
        print(f"FAILED {company['ticker']}: {exc}", flush=True)


def worker(db_path: Path, year: int, quarter: str, high_confidence: float, limit: int | None, counter: list[int], lock: threading.Lock) -> None:
    conn = connect(db_path)
    while True:
        with lock:
            if limit is not None and counter[0] >= limit:
                break
            company = claim(conn)
            if company is None:
                break
            counter[0] += 1
        process_one(conn, company, year, quarter, high_confidence)
        print(f"{company['ticker']} completed ({counter[0]})", flush=True)
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, required=True); parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--year", type=int, default=2026); parser.add_argument("--quarter", default="Q1")
    parser.add_argument("--workers", type=int, default=3); parser.add_argument("--limit", type=int)
    parser.add_argument("--high-confidence", type=float, default=.90); parser.add_argument("--reset-running", action="store_true")
    args = parser.parse_args()
    initialize(args.csv, args.db)
    if args.reset_running:
        conn = connect(args.db); conn.execute("UPDATE companies SET status='pending', error=NULL WHERE status='running'"); conn.close()
    counter, lock = [0], threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(worker, args.db, args.year, args.quarter, args.high_confidence, args.limit, counter, lock) for _ in range(args.workers)]
        for future in futures:
            future.result()
    conn = connect(args.db)
    print(dict(conn.execute("SELECT status, COUNT(*) AS count FROM companies GROUP BY status").fetchall()))
    print({"accepted_urls": conn.execute("SELECT COUNT(*) FROM accepted_urls").fetchone()[0]})
    conn.close()


if __name__ == "__main__":
    main()
