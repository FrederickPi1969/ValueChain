"""Eight-worker asyncio runner for the resumable earnings-call SQLite queue."""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path

from run_earnings_call_batch import claim, connect, initialize, now
from valuechain.earnings_calls import Settings, _rotating_proxy, best_bet_queries, eligible, judge_candidates_async, search_query


async def process_one(conn, company, year: int, quarter: str, high_confidence: float) -> None:
    company_id, target = company["id"], f"{company['company_name']} ({company['ticker']})"
    settings = Settings()
    proxies = settings.proxies or await asyncio.to_thread(_rotating_proxy)
    query_count = 0
    try:
        for ordinal, query in enumerate(best_bet_queries(target, year, quarter), 1):
            engine, found = await asyncio.to_thread(search_query, query, proxies=proxies)
            query_count += 1
            conn.execute("INSERT OR IGNORE INTO search_queries(company_id, ordinal, query, engine, result_count, searched_at) VALUES (?, ?, ?, ?, ?, ?)", (company_id, ordinal, query, engine, len(found), now()))
            qid = conn.execute("SELECT id FROM search_queries WHERE company_id=? AND ordinal=?", (company_id, ordinal)).fetchone()[0]
            ids = []
            for i, candidate in enumerate(found):
                conn.execute("INSERT OR IGNORE INTO candidates(search_query_id, ordinal, url, title, snippet, source_type) VALUES (?, ?, ?, ?, ?, ?)", (qid, i, candidate.url, candidate.title, candidate.snippet, candidate.source_type))
                ids.append(conn.execute("SELECT id FROM candidates WHERE search_query_id=? AND ordinal=?", (qid, i)).fetchone()[0])
            judgements = await judge_candidates_async(target, year, quarter, found, settings)
            early = False
            for j in judgements:
                candidate, candidate_id = found[j.candidate_index], ids[j.candidate_index]
                conn.execute("INSERT OR REPLACE INTO judgements(candidate_id, is_target, confidence, content_kind, reason, judged_at) VALUES (?, ?, ?, ?, ?, ?)", (candidate_id, int(j.is_target), j.confidence, j.content_kind, j.reason, now()))
                if eligible(candidate, j):
                    conn.execute("INSERT OR IGNORE INTO accepted_urls(company_id, candidate_id, url, title, confidence, content_kind, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (company_id, candidate_id, candidate.url, candidate.title, j.confidence, j.content_kind, now()))
                    early = early or j.confidence >= high_confidence
            if early:
                conn.execute("UPDATE search_queries SET stopped_early=1 WHERE id=?", (qid,))
                break
        conn.execute("UPDATE companies SET status='completed', query_count=?, updated_at=?, error=NULL WHERE id=?", (query_count, now(), company_id))
        print(f"{company['ticker']} completed", flush=True)
    except Exception as exc:
        conn.execute("UPDATE companies SET status='failed', query_count=?, updated_at=?, error=? WHERE id=?", (query_count, now(), f"{type(exc).__name__}: {exc}", company_id))
        print(f"FAILED {company['ticker']}: {exc}", flush=True)


async def worker(db: Path, year: int, quarter: str, confidence: float, limit: int | None, claimed: list[int], lock: asyncio.Lock) -> None:
    conn = connect(db)
    while True:
        async with lock:
            if limit is not None and claimed[0] >= limit:
                break
            company = claim(conn)
            if company is None:
                break
            claimed[0] += 1
        await process_one(conn, company, year, quarter, confidence)
    conn.close()


async def run(args) -> None:
    if args.csv:
        initialize(args.csv, args.db)
    else:
        # A source queue is an audited substitute when the original uploaded
        # CSV has been moved.  Only company identity fields are copied; search
        # state and candidate URLs never cross reporting periods.
        seed = sqlite3.connect(args.seed_db)
        rows = seed.execute("SELECT ticker, company_name, sector FROM companies ORDER BY id").fetchall()
        seed.close()
        conn = connect(args.db)
        for ticker, company_name, sector in rows:
            conn.execute("INSERT OR IGNORE INTO companies(ticker, company_name, sector, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (ticker, company_name, sector, now(), now()))
        conn.close()
    if args.reset_running:
        conn = connect(args.db); conn.execute("UPDATE companies SET status='pending', error=NULL WHERE status='running'"); conn.close()
    if args.retry_failed:
        conn = connect(args.db); conn.execute("UPDATE companies SET status='pending', error=NULL WHERE status='failed'"); conn.close()
    claimed, lock = [0], asyncio.Lock()
    await asyncio.gather(*(worker(args.db, args.year, args.quarter, args.high_confidence, args.limit, claimed, lock) for _ in range(args.workers)))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=Path); p.add_argument("--seed-db", type=Path); p.add_argument("--db", type=Path, required=True)
    p.add_argument("--year", type=int, default=2026); p.add_argument("--quarter", default="Q1")
    p.add_argument("--workers", type=int, default=8); p.add_argument("--limit", type=int); p.add_argument("--high-confidence", type=float, default=.90); p.add_argument("--reset-running", action="store_true"); p.add_argument("--retry-failed", action="store_true")
    args = p.parse_args()
    if not args.csv and not args.seed_db:
        p.error("one of --csv or --seed-db is required")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
