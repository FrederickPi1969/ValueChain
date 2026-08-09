"""DuckDuckGo recovery for exhausted earnings-call companies (no Google usage)."""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import time
from pathlib import Path

import requests

from run_earnings_call_batch import connect, now
from valuechain.earnings_calls import Candidate, Settings, eligible, judge_candidates_async

DDG_BASE = "https://serp.frederickpi.com"


def ddg(query: str) -> list[Candidate]:
    submitted = requests.post(f"{DDG_BASE}/search", json={"query": query, "pages": 1}, timeout=30)
    submitted.raise_for_status()
    task_id = submitted.json().get("task_id")
    if not task_id:
        raise RuntimeError("DDG service did not return task_id")
    for _ in range(75):
        time.sleep(1)
        status = requests.get(f"{DDG_BASE}/search/{task_id}/status", timeout=20).json()
        if status.get("status") == "SUCCESS":
            payload = requests.get(f"{DDG_BASE}/search/{task_id}/result", timeout=30).json()
            raw = payload.get("results", {})
            records = raw.values() if isinstance(raw, dict) else raw
            return [Candidate(str(item.get("url") or item.get("link") or ""), str(item.get("title") or ""), str(item.get("snippet") or item.get("summary") or item.get("text") or ""), "duckduckgo_serp", query) for item in records if isinstance(item, dict) and (item.get("url") or item.get("link"))][:10]
        if status.get("status") == "FAILURE":
            raise RuntimeError(str(status.get("error") or status))
    raise TimeoutError("DDG service polling timed out")


def initialize(db: Path, retry_misses: bool) -> None:
    conn = connect(db)
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS ddg_conference_recovery (
        company_id INTEGER PRIMARY KEY REFERENCES companies(id), status TEXT NOT NULL DEFAULT 'pending',
        error TEXT, updated_at TEXT NOT NULL
      );
    """)
    conn.execute("""INSERT OR IGNORE INTO ddg_conference_recovery(company_id,status,updated_at)
                    SELECT s.company_id,'pending',? FROM pathfinder_company_status s WHERE s.status='exhausted'""", (now(),))
    if retry_misses:
        # Revisit only companies whose first-priority conference-call query
        # found no accepted candidate.  Existing successes never spend the
        # lower-priority query slots.
        conn.execute("""UPDATE ddg_conference_recovery SET status='pending',error=NULL,updated_at=?
                        WHERE status='completed' AND NOT EXISTS (
                          SELECT 1 FROM accepted_urls a JOIN candidates x ON x.id=a.candidate_id
                          JOIN search_queries q ON q.id=x.search_query_id
                          WHERE a.company_id=ddg_conference_recovery.company_id AND q.engine='duckduckgo_serp'
                        )""", (now(),))
    conn.close()


def claim(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("""SELECT r.company_id,c.ticker,c.company_name FROM ddg_conference_recovery r
                          JOIN companies c ON c.id=r.company_id WHERE r.status='pending' ORDER BY r.company_id LIMIT 1""").fetchone()
    if row:
        conn.execute("UPDATE ddg_conference_recovery SET status='running',updated_at=? WHERE company_id=?", (now(), row["company_id"]))
    conn.execute("COMMIT")
    return row


async def process(conn: sqlite3.Connection, row, year: int, quarter: str) -> None:
    target = f"{row['company_name']} ({row['ticker']})"
    try:
        accepted = False
        base = f"{row['ticker']} {row['company_name']} {year} {quarter}"
        # Requested priority: conference call, then an explicit YouTube
        # suffix, only then transcript/results keyword variations.
        for query_ordinal, query in (
            (100, f"{base} earnings conference call"),
            (101, f"{base} earnings conference call YouTube"),
            (102, f"{base} earnings call transcript"),
            (103, f"{base} quarterly results conference call"),
        ):
            if accepted:
                break
            found = await asyncio.to_thread(ddg, query)
            conn.execute("INSERT OR IGNORE INTO search_queries(company_id,ordinal,query,engine,result_count,searched_at) VALUES(?,?,?,?,?,?)", (row["company_id"], query_ordinal, query, "duckduckgo_serp", len(found), now()))
            qid = conn.execute("SELECT id FROM search_queries WHERE company_id=? AND ordinal=?", (row["company_id"], query_ordinal)).fetchone()[0]
            ids = []
            for ordinal, candidate in enumerate(found):
                conn.execute("INSERT OR IGNORE INTO candidates(search_query_id,ordinal,url,title,snippet,source_type) VALUES(?,?,?,?,?,?)", (qid, ordinal, candidate.url, candidate.title, candidate.snippet, candidate.source_type))
                ids.append(conn.execute("SELECT id FROM candidates WHERE search_query_id=? AND ordinal=?", (qid, ordinal)).fetchone()[0])
            judgements = await judge_candidates_async(target, year, quarter, found, Settings())
            for verdict in judgements:
                candidate, candidate_id = found[verdict.candidate_index], ids[verdict.candidate_index]
                conn.execute("INSERT OR REPLACE INTO judgements(candidate_id,is_target,confidence,content_kind,reason,judged_at) VALUES(?,?,?,?,?,?)", (candidate_id, int(verdict.is_target), verdict.confidence, verdict.content_kind, verdict.reason, now()))
                if eligible(candidate, verdict):
                    conn.execute("INSERT OR IGNORE INTO accepted_urls(company_id,candidate_id,url,title,confidence,content_kind,accepted_at) VALUES(?,?,?,?,?,?,?)", (row["company_id"], candidate_id, candidate.url, candidate.title, verdict.confidence, verdict.content_kind, now()))
                    accepted = True
        if accepted:
            conn.execute("DELETE FROM pathfinder_company_status WHERE company_id=? AND status='exhausted'", (row["company_id"],))
        conn.execute("UPDATE ddg_conference_recovery SET status='completed',error=NULL,updated_at=? WHERE company_id=?", (now(), row["company_id"]))
        print(f"DDG {row['ticker']} accepted={accepted}", flush=True)
    except Exception as exc:
        conn.execute("UPDATE ddg_conference_recovery SET status='failed',error=?,updated_at=? WHERE company_id=?", (f"{type(exc).__name__}: {exc}", now(), row["company_id"]))
        print(f"DDG FAILED {row['ticker']}: {exc}", flush=True)


async def worker(db: Path, year: int, quarter: str) -> None:
    conn = connect(db)
    while (row := claim(conn)) is not None:
        await process(conn, row, year, quarter)
    conn.close()


async def run(args) -> None:
    initialize(args.db, args.retry_misses)
    await asyncio.gather(*(worker(args.db, args.year, args.quarter) for _ in range(args.workers)))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--db', type=Path, required=True); p.add_argument('--year', type=int, default=2026); p.add_argument('--quarter', default='Q1'); p.add_argument('--workers', type=int, default=8); p.add_argument('--retry-misses', action='store_true')
    asyncio.run(run(p.parse_args()))


if __name__ == '__main__':
    main()
