from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from valuechain.acquisition_schedule import rescan_window_start


@dataclass(frozen=True)
class AcquisitionIssuer:
    cik: str
    ticker: str
    company_name: str
    exchange: str
    priority: int


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS issuers (
    cik TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    company_name TEXT NOT NULL,
    exchange TEXT NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    scanned_at TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_issuers_queue
ON issuers(status, priority, next_attempt_at, scanned_at);

CREATE TABLE IF NOT EXISTS issuer_year_scans (
    cik TEXT NOT NULL,
    filing_year INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    scanned_at TEXT,
    last_error TEXT,
    PRIMARY KEY(cik, filing_year),
    FOREIGN KEY(cik) REFERENCES issuers(cik)
);

CREATE INDEX IF NOT EXISTS idx_issuer_year_scan_queue
ON issuer_year_scans(filing_year, status, next_attempt_at, scanned_at);

CREATE TABLE IF NOT EXISTS filings (
    accession_number TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    form TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    report_date TEXT,
    accepted_at TEXT,
    primary_document TEXT,
    archive_url TEXT NOT NULL,
    local_dir TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    status TEXT NOT NULL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_filings_cik_date
ON filings(cik, filing_date);

CREATE TABLE IF NOT EXISTS documents (
    source_url TEXT PRIMARY KEY,
    accession_number TEXT NOT NULL,
    document_kind TEXT NOT NULL,
    local_path TEXT NOT NULL,
    content_type TEXT,
    byte_size INTEGER,
    sha256 TEXT,
    retrieved_at TEXT,
    status TEXT NOT NULL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS acquisition_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    issuer_count INTEGER NOT NULL DEFAULT 0,
    filing_count INTEGER NOT NULL DEFAULT 0,
    document_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0
);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


class AcquisitionState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> AcquisitionState:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def upsert_issuers(self, issuers: Iterable[AcquisitionIssuer]) -> int:
        rows = list(issuers)
        self.connection.executemany(
            """
            INSERT INTO issuers(cik, ticker, company_name, exchange, priority)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cik) DO UPDATE SET
                ticker = excluded.ticker,
                company_name = excluded.company_name,
                exchange = excluded.exchange,
                priority = MIN(issuers.priority, excluded.priority)
            """,
            [(row.cik, row.ticker, row.company_name, row.exchange, row.priority) for row in rows],
        )
        self.connection.commit()
        return len(rows)

    def ensure_scan_years(self, years: Iterable[int]) -> None:
        for year in years:
            if year == 2026:
                self.connection.execute(
                    """
                    INSERT INTO issuer_year_scans(
                        cik, filing_year, status, attempts, next_attempt_at, scanned_at, last_error
                    )
                    SELECT cik, 2026, status, attempts, next_attempt_at, scanned_at, last_error
                    FROM issuers
                    WHERE true
                    ON CONFLICT(cik, filing_year) DO NOTHING
                    """
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO issuer_year_scans(cik, filing_year, status)
                    SELECT cik, ?, 'pending' FROM issuers
                    WHERE true
                    ON CONFLICT(cik, filing_year) DO NOTHING
                    """,
                    (year,),
                )
        self.connection.commit()

    def claim_issuers(
        self,
        limit: int,
        filing_year: int = 2026,
        rescan_hours: int | None = None,
    ) -> list[AcquisitionIssuer]:
        now = utc_now()
        now_text = now.isoformat()
        rescan_before = (
            rescan_window_start(now, rescan_hours).isoformat()
            if rescan_hours is not None
            else ""
        )
        rows = self.connection.execute(
            """
            SELECT i.cik, i.ticker, i.company_name, i.exchange, i.priority
            FROM issuer_year_scans ys
            JOIN issuers i ON i.cik = ys.cik
            WHERE ys.filing_year = ?
              AND (
                (ys.status IN ('pending', 'retry') AND (
                    ys.next_attempt_at IS NULL OR ys.next_attempt_at <= ?
                ))
                OR (? IS NOT NULL AND ys.status = 'complete' AND ys.scanned_at <= ?)
                OR ys.status = 'running'
              )
            ORDER BY
                CASE ys.status
                    WHEN 'running' THEN 0 WHEN 'pending' THEN 1
                    WHEN 'retry' THEN 2 ELSE 3
                END,
                i.priority,
                COALESCE(ys.scanned_at, ''),
                i.ticker
            LIMIT ?
            """,
            (filing_year, now_text, rescan_hours, rescan_before, limit),
        ).fetchall()
        issuers = [AcquisitionIssuer(**dict(row)) for row in rows]
        self.connection.executemany(
            """
            UPDATE issuer_year_scans
            SET status = 'running', attempts = attempts + 1
            WHERE cik = ? AND filing_year = ?
            """,
            [(issuer.cik, filing_year) for issuer in issuers],
        )
        self.connection.commit()
        return issuers

    def complete_issuer(self, cik: str, filing_year: int = 2026) -> None:
        self.connection.execute(
            """
            UPDATE issuer_year_scans
            SET status = 'complete', scanned_at = ?, next_attempt_at = NULL, last_error = NULL
            WHERE cik = ? AND filing_year = ?
            """,
            (iso_now(), cik, filing_year),
        )
        if filing_year == 2026:
            self.connection.execute(
                """
                UPDATE issuers
                SET status = 'complete', scanned_at = ?, next_attempt_at = NULL, last_error = NULL
                WHERE cik = ?
                """,
                (iso_now(), cik),
            )
        self.connection.commit()

    def fail_issuer(
        self,
        cik: str,
        error: str,
        filing_year: int = 2026,
        retry_minutes: int = 30,
    ) -> None:
        next_attempt = (utc_now() + timedelta(minutes=retry_minutes)).isoformat()
        self.connection.execute(
            """
            UPDATE issuer_year_scans
            SET status = 'retry', next_attempt_at = ?, last_error = ?
            WHERE cik = ? AND filing_year = ?
            """,
            (next_attempt, error[:1000], cik, filing_year),
        )
        if filing_year == 2026:
            self.connection.execute(
                """
                UPDATE issuers
                SET status = 'retry', next_attempt_at = ?, last_error = ?
                WHERE cik = ?
                """,
                (next_attempt, error[:1000], cik),
            )
        self.connection.commit()

    def year_progress(self, filing_year: int) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM issuer_year_scans
            WHERE filing_year = ?
            GROUP BY status ORDER BY status
            """,
            (filing_year,),
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def active_backfill_year(self, years: Iterable[int]) -> int | None:
        for year in years:
            progress = self.year_progress(year)
            incomplete = sum(
                count for status, count in progress.items() if status != "complete"
            )
            if not progress or incomplete:
                return year
        return None

    def rescan_due(self, filing_year: int, rescan_hours: int) -> bool:
        cutoff = rescan_window_start(datetime.now(UTC), rescan_hours)
        row = self.connection.execute(
            """
            SELECT 1 FROM issuer_year_scans
            WHERE filing_year = ? AND (
              status IN ('pending', 'running')
              OR (status = 'retry' AND (
                next_attempt_at IS NULL OR next_attempt_at <= ?
              ))
              OR (status = 'complete' AND (
                scanned_at IS NULL OR scanned_at <= ?
              ))
            )
            LIMIT 1
            """,
            (filing_year, iso_now(), cutoff.isoformat()),
        ).fetchone()
        return row is not None

    def upsert_filing(self, filing: dict, local_dir: Path, status: str, error: str = "") -> None:
        self.connection.execute(
            """
            INSERT INTO filings(
                accession_number, cik, form, filing_date, report_date, accepted_at,
                primary_document, archive_url, local_dir, discovered_at, status, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(accession_number) DO UPDATE SET
                primary_document = excluded.primary_document,
                local_dir = excluded.local_dir,
                status = excluded.status,
                last_error = excluded.last_error
            """,
            (
                filing["accession_number"],
                filing["cik"],
                filing["form"],
                filing["filing_date"],
                filing.get("report_date", ""),
                filing.get("accepted_at", ""),
                filing.get("primary_document", ""),
                filing["archive_url"],
                str(local_dir),
                iso_now(),
                status,
                error[:1000],
            ),
        )
        self.connection.commit()

    def upsert_document(self, document: dict) -> None:
        self.connection.execute(
            """
            INSERT INTO documents(
                source_url, accession_number, document_kind, local_path, content_type,
                byte_size, sha256, retrieved_at, status, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_url) DO UPDATE SET
                local_path = excluded.local_path,
                content_type = excluded.content_type,
                byte_size = excluded.byte_size,
                sha256 = excluded.sha256,
                retrieved_at = excluded.retrieved_at,
                status = excluded.status,
                last_error = excluded.last_error
            """,
            (
                document["source_url"],
                document["accession_number"],
                document["document_kind"],
                document["local_path"],
                document.get("content_type", ""),
                document.get("byte_size"),
                document.get("sha256", ""),
                document.get("retrieved_at", iso_now()),
                document["status"],
                document.get("last_error", "")[:1000],
            ),
        )
        self.connection.commit()

    def begin_run(self, run_id: str) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_runs
            SET completed_at = ?, status = 'interrupted'
            WHERE status = 'running'
            """,
            (iso_now(),),
        )
        self.connection.execute(
            "INSERT INTO acquisition_runs(run_id, started_at, status) VALUES (?, ?, 'running')",
            (run_id, iso_now()),
        )
        self.connection.commit()

    def finish_run(self, run_id: str, status: str, counts: dict[str, int]) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_runs
            SET completed_at = ?, status = ?, issuer_count = ?, filing_count = ?,
                document_count = ?, error_count = ?
            WHERE run_id = ?
            """,
            (
                iso_now(),
                status,
                counts.get("issuers", 0),
                counts.get("filings", 0),
                counts.get("documents", 0),
                counts.get("errors", 0),
                run_id,
            ),
        )
        self.connection.commit()

    def stats(self) -> dict[str, object]:
        issuer_rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM issuers GROUP BY status ORDER BY status"
        ).fetchall()
        totals = self.connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM filings) AS filings,
              (SELECT COUNT(*) FROM documents WHERE status = 'complete') AS documents,
              (SELECT COALESCE(SUM(byte_size), 0) FROM documents WHERE status = 'complete') AS bytes
            """
        ).fetchone()
        latest_run = self.connection.execute(
            "SELECT * FROM acquisition_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        year_rows = self.connection.execute(
            """
            SELECT filing_year, status, COUNT(*) AS count
            FROM issuer_year_scans
            GROUP BY filing_year, status
            ORDER BY filing_year DESC, status
            """
        ).fetchall()
        years: dict[str, dict[str, int]] = {}
        for row in year_rows:
            years.setdefault(str(row["filing_year"]), {})[row["status"]] = row["count"]
        return {
            "issuers": {row["status"]: row["count"] for row in issuer_rows},
            "filings": totals["filings"],
            "documents": totals["documents"],
            "bytes": totals["bytes"],
            "issuer_years": years,
            "latest_run": dict(latest_run) if latest_run else None,
        }
