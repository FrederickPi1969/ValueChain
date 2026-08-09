"""Use one remaining bounded-search slot for YouTube on exhausted companies."""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path

from run_earnings_call_batch import connect, now
from valuechain.earnings_calls import Settings, _rotating_proxy, eligible, judge_candidates_async, search_query


def initialise(db: Path, retry_misses: bool) -> None:
    conn = connect(db)
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS youtube_backfill (
        company_id INTEGER PRIMARY KEY REFERENCES companies(id), status TEXT NOT NULL DEFAULT 'pending',
        query_ordinal INTEGER NOT NULL, error TEXT, updated_at TEXT NOT NULL
      );
    """)
    conn.execute("""INSERT OR IGNORE INTO youtube_backfill(company_id,status,query_ordinal,updated_at)
                    SELECT c.id,'pending',c.query_count+1,?
                    FROM companies c JOIN pathfinder_company_status s ON s.company_id=c.id
                    WHERE s.status='exhausted' AND c.query_count<4""", (now(),))
    if retry_misses:
        # The original query quoted `Company (TICKER)` as a single phrase.
        # YouTube titles usually omit the parenthetical ticker (e.g. BlackRock),
        # so retry misses only when one of the four bounded slots remains.
        conn.execute("""UPDATE youtube_backfill
                        SET status='pending', query_ordinal=(SELECT query_count+1 FROM companies c WHERE c.id=youtube_backfill.company_id), error=NULL, updated_at=?
                        WHERE status='completed'
                          AND NOT EXISTS (SELECT 1 FROM accepted_urls a WHERE a.company_id=youtube_backfill.company_id AND a.content_kind='youtube_video')
                          AND (SELECT query_count FROM companies c WHERE c.id=youtube_backfill.company_id) < 4""", (now(),))
    conn.close()


def claim(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("""SELECT b.company_id,b.query_ordinal,c.ticker,c.company_name
                          FROM youtube_backfill b JOIN companies c ON c.id=b.company_id
                          WHERE b.status='pending' ORDER BY b.company_id LIMIT 1""").fetchone()
    if row:
        conn.execute("UPDATE youtube_backfill SET status='running',updated_at=? WHERE company_id=?", (now(), row["company_id"]))
    conn.execute("COMMIT")
    return row


async def one(conn: sqlite3.Connection, row, year: int, quarter: str) -> None:
    target = f"{row['company_name']} ({row['ticker']})"
    # Do not quote a parenthetical ticker together with the legal company
    # name: earnings-call videos generally title themselves with the company
    # name alone.  This is still exactly one bounded Google query.
    query = f'site:youtube.com "{row["company_name"]}" "{quarter} {year}" "earnings call"'
    settings = Settings()
    try:
        engine, found = await asyncio.to_thread(search_query, query, proxies=settings.proxies or await asyncio.to_thread(_rotating_proxy))
        conn.execute("INSERT OR IGNORE INTO search_queries(company_id,ordinal,query,engine,result_count,searched_at) VALUES(?,?,?,?,?,?)", (row["company_id"], row["query_ordinal"], query, engine, len(found), now()))
        qid = conn.execute("SELECT id FROM search_queries WHERE company_id=? AND ordinal=?", (row["company_id"], row["query_ordinal"])).fetchone()[0]
        ids = []
        for ordinal, candidate in enumerate(found):
            conn.execute("INSERT OR IGNORE INTO candidates(search_query_id,ordinal,url,title,snippet,source_type) VALUES(?,?,?,?,?,?)", (qid, ordinal, candidate.url, candidate.title, candidate.snippet, candidate.source_type))
            ids.append(conn.execute("SELECT id FROM candidates WHERE search_query_id=? AND ordinal=?", (qid, ordinal)).fetchone()[0])
        judgements = await judge_candidates_async(target, year, quarter, found, settings)
        accepted = False
        for judgement in judgements:
            candidate, candidate_id = found[judgement.candidate_index], ids[judgement.candidate_index]
            conn.execute("INSERT OR REPLACE INTO judgements(candidate_id,is_target,confidence,content_kind,reason,judged_at) VALUES(?,?,?,?,?,?)", (candidate_id, int(judgement.is_target), judgement.confidence, judgement.content_kind, judgement.reason, now()))
            if "youtube.com" in candidate.url.lower() and eligible(candidate, judgement):
                conn.execute("INSERT OR IGNORE INTO accepted_urls(company_id,candidate_id,url,title,confidence,content_kind,accepted_at) VALUES(?,?,?,?,?,?,?)", (row["company_id"], candidate_id, candidate.url, candidate.title, judgement.confidence, "youtube_video", now()))
                accepted = True
        conn.execute("UPDATE companies SET query_count=?,updated_at=? WHERE id=?", (row["query_ordinal"], now(), row["company_id"]))
        if accepted:
            conn.execute("DELETE FROM pathfinder_company_status WHERE company_id=? AND status='exhausted'", (row["company_id"],))
        conn.execute("UPDATE youtube_backfill SET status='completed',error=NULL,updated_at=? WHERE company_id=?", (now(), row["company_id"]))
        print(f"YOUTUBE {row['ticker']} accepted={accepted}", flush=True)
    except Exception as exc:
        conn.execute("UPDATE youtube_backfill SET status='failed',error=?,updated_at=? WHERE company_id=?", (f"{type(exc).__name__}: {exc}", now(), row["company_id"]))
        print(f"YOUTUBE FAILED {row['ticker']}: {exc}", flush=True)


async def worker(db: Path, year: int, quarter: str) -> None:
    conn = connect(db)
    while (row := claim(conn)) is not None:
        await one(conn, row, year, quarter)
    conn.close()


async def run(args) -> None:
    initialise(args.db, args.retry_misses)
    await asyncio.gather(*(worker(args.db, args.year, args.quarter) for _ in range(args.workers)))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--db', type=Path, required=True); p.add_argument('--year', type=int, default=2026); p.add_argument('--quarter', default='Q1'); p.add_argument('--workers', type=int, default=8); p.add_argument('--retry-misses', action='store_true')
    asyncio.run(run(p.parse_args()))


if __name__ == '__main__':
    main()
