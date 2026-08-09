"""One remaining Google-query-slot recovery using the conference-call format."""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path

from run_earnings_call_batch import connect, now
from valuechain.earnings_calls import Settings, _rotating_proxy, eligible, judge_candidates_async, search_query


def initialize(db: Path) -> None:
    conn = connect(db)
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS google_conference_recovery (
        company_id INTEGER PRIMARY KEY REFERENCES companies(id), status TEXT NOT NULL DEFAULT 'pending',
        query_ordinal INTEGER NOT NULL, error TEXT, updated_at TEXT NOT NULL
      );
    """)
    # Stale old runners may have left a row marked running.  Its stored count
    # is preserved; the new recovery consumes exactly the next available slot.
    conn.execute("UPDATE companies SET status='completed',error=NULL,updated_at=? WHERE status='running'", (now(),))
    conn.execute("""INSERT OR IGNORE INTO google_conference_recovery(company_id,status,query_ordinal,updated_at)
                    SELECT id,'pending',query_count+1,? FROM companies WHERE query_count<4""", (now(),))
    conn.close()


def claim(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("""SELECT r.company_id,r.query_ordinal,c.ticker,c.company_name
                          FROM google_conference_recovery r JOIN companies c ON c.id=r.company_id
                          WHERE r.status='pending' ORDER BY r.company_id LIMIT 1""").fetchone()
    if row:
        conn.execute("UPDATE google_conference_recovery SET status='running',updated_at=? WHERE company_id=?", (now(), row['company_id']))
    conn.execute("COMMIT")
    return row


async def process(conn: sqlite3.Connection, row, year: int, quarter: str) -> None:
    target = f"{row['company_name']} ({row['ticker']})"
    query = f"{row['ticker']} {row['company_name']} {year} {quarter} earnings conference call"
    try:
        settings = Settings()
        engine, found = await asyncio.to_thread(search_query, query, proxies=settings.proxies or await asyncio.to_thread(_rotating_proxy))
        conn.execute("INSERT OR IGNORE INTO search_queries(company_id,ordinal,query,engine,result_count,searched_at) VALUES(?,?,?,?,?,?)", (row['company_id'], row['query_ordinal'], query, engine, len(found), now()))
        qid = conn.execute("SELECT id FROM search_queries WHERE company_id=? AND ordinal=?", (row['company_id'], row['query_ordinal'])).fetchone()[0]
        ids=[]
        for ordinal, candidate in enumerate(found):
            conn.execute("INSERT OR IGNORE INTO candidates(search_query_id,ordinal,url,title,snippet,source_type) VALUES(?,?,?,?,?,?)", (qid,ordinal,candidate.url,candidate.title,candidate.snippet,candidate.source_type))
            ids.append(conn.execute("SELECT id FROM candidates WHERE search_query_id=? AND ordinal=?", (qid,ordinal)).fetchone()[0])
        accepted=False
        for verdict in await judge_candidates_async(target, year, quarter, found, settings):
            candidate, cid=found[verdict.candidate_index], ids[verdict.candidate_index]
            conn.execute("INSERT OR REPLACE INTO judgements(candidate_id,is_target,confidence,content_kind,reason,judged_at) VALUES(?,?,?,?,?,?)", (cid,int(verdict.is_target),verdict.confidence,verdict.content_kind,verdict.reason,now()))
            if eligible(candidate, verdict):
                conn.execute("INSERT OR IGNORE INTO accepted_urls(company_id,candidate_id,url,title,confidence,content_kind,accepted_at) VALUES(?,?,?,?,?,?,?)", (row['company_id'],cid,candidate.url,candidate.title,verdict.confidence,verdict.content_kind,now()))
                accepted=True
        conn.execute("UPDATE companies SET query_count=?,updated_at=? WHERE id=?", (row['query_ordinal'],now(),row['company_id']))
        conn.execute("UPDATE google_conference_recovery SET status='completed',error=NULL,updated_at=? WHERE company_id=?", (now(),row['company_id']))
        print(f"GOOGLE {row['ticker']} accepted={accepted}",flush=True)
    except Exception as exc:
        conn.execute("UPDATE google_conference_recovery SET status='failed',error=?,updated_at=? WHERE company_id=?", (f"{type(exc).__name__}: {exc}",now(),row['company_id']))
        print(f"GOOGLE FAILED {row['ticker']}: {exc}",flush=True)


async def worker(db: Path, year: int, quarter: str) -> None:
    conn=connect(db)
    while (row:=claim(conn)) is not None:
        await process(conn,row,year,quarter)
    conn.close()


async def run(args):
    initialize(args.db)
    await asyncio.gather(*(worker(args.db,args.year,args.quarter) for _ in range(args.workers)))


def main():
    p=argparse.ArgumentParser(); p.add_argument('--db',type=Path,required=True); p.add_argument('--year',type=int,default=2026); p.add_argument('--quarter',default='Q1'); p.add_argument('--workers',type=int,default=8)
    asyncio.run(run(p.parse_args()))

if __name__=='__main__': main()
