"""Promote already-saved, non-paywall search results for downloader recovery.

No search request is issued.  The content validator remains the final gate;
this only lets Pathfinder try alternatives that were saved in the same bounded
four-query search budget but were not Qwen's first link-level choice.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--quarter", default="Q1")
    p.add_argument("--per-company", type=int, default=4)
    args = p.parse_args()
    db = sqlite3.connect(args.db, timeout=60, isolation_level=None)
    db.execute("PRAGMA busy_timeout=60000")
    year, quarter = str(args.year), args.quarter.lower()
    # The ranking rewards directly fetchable transcript/webcast endpoints and
    # still permits corporate IR hosts, while permanently excluding sources
    # demonstrated to serve indexes/paywalled fragments.
    db.execute(f"""
      WITH candidate_pool AS (
        SELECT q.company_id, x.id candidate_id, x.url, x.title,
               ROW_NUMBER() OVER (
                 PARTITION BY q.company_id ORDER BY
                 CASE
                   WHEN lower(x.url) LIKE '%finance.yahoo.com/quote/%/earnings/%' THEN 0
                   WHEN lower(x.url) LIKE '%roic.ai/%/transcripts/%' THEN 1
                   WHEN lower(x.url) LIKE '%transcript%' THEN 2
                   WHEN lower(x.url) LIKE '%webcast%' OR lower(x.url) LIKE '%.pdf%' THEN 3
                   WHEN lower(x.url) LIKE '%/ir/%' OR lower(x.url) LIKE '%investor%' THEN 4
                   ELSE 9 END,
                 q.ordinal, x.ordinal
               ) AS rank
        FROM candidates x JOIN search_queries q ON q.id=x.search_query_id
        LEFT JOIN accepted_urls a ON a.candidate_id=x.id
        WHERE a.id IS NULL
          AND lower(x.url) NOT LIKE '%seekingalpha.com%'
          AND lower(x.url) NOT LIKE '%gurufocus.com%'
          AND lower(x.url) NOT LIKE '%fool.com%'
          AND (lower(x.title || ' ' || x.snippet || ' ' || x.url) LIKE '%{quarter}%'
               OR lower(x.title || ' ' || x.snippet || ' ' || x.url) LIKE '%first quarter%')
          AND lower(x.title || ' ' || x.snippet || ' ' || x.url) LIKE '%{year}%'
          AND (lower(x.title || ' ' || x.snippet || ' ' || x.url) LIKE '%earnings call%'
               OR lower(x.title || ' ' || x.snippet || ' ' || x.url) LIKE '%earnings transcript%'
               OR lower(x.title || ' ' || x.snippet || ' ' || x.url) LIKE '%results webcast%'
               OR lower(x.title || ' ' || x.snippet || ' ' || x.url) LIKE '%conference call%')
      )
      INSERT OR IGNORE INTO accepted_urls(company_id,candidate_id,url,title,confidence,content_kind,accepted_at)
      SELECT company_id,candidate_id,url,title,0.55,'recovery_candidate',datetime('now')
      FROM candidate_pool WHERE rank <= ?
    """, (args.per_company,))
    promoted = db.execute("SELECT changes()").fetchone()[0]
    # Reopen only companies previously exhausted: existing validated calls
    # stay immutable and recovery cannot replace them.
    db.execute("""DELETE FROM pathfinder_company_status
                  WHERE status='exhausted' AND company_id IN
                    (SELECT DISTINCT company_id FROM accepted_urls WHERE content_kind='recovery_candidate')""")
    db.close()
    print(f"promoted={promoted}")


if __name__ == '__main__':
    main()
