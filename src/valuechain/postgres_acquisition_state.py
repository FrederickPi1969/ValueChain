from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from valuechain.acquisition_state import AcquisitionIssuer
from valuechain.acquisition_schedule import rescan_window_start
from valuechain.acquisition_schema import ensure_acquisition_schema

SOURCE_ID = "sec_edgar"


def utc_now() -> datetime:
    return datetime.now(UTC)


class PostgresAcquisitionState:
    def __init__(
        self,
        database_url: str,
        source_id: str = SOURCE_ID,
        ensure_schema: bool = True,
    ) -> None:
        self.database_url = database_url
        self.source_id = source_id
        self.connection = psycopg.connect(database_url, row_factory=dict_row)
        if ensure_schema:
            ensure_acquisition_schema(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> PostgresAcquisitionState:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def upsert_issuers(self, issuers: Iterable[AcquisitionIssuer]) -> int:
        rows = list(issuers)
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO acquisition_issuers(
                    source_id, source_issuer_id, ticker, company_name, exchange, priority
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(source_id, source_issuer_id) DO UPDATE SET
                    ticker = EXCLUDED.ticker,
                    company_name = EXCLUDED.company_name,
                    exchange = EXCLUDED.exchange,
                    priority = LEAST(acquisition_issuers.priority, EXCLUDED.priority),
                    updated_at = now()
                """,
                [
                    (
                        self.source_id,
                        row.cik,
                        row.ticker,
                        row.company_name,
                        row.exchange,
                        row.priority,
                    )
                    for row in rows
                ],
            )
        self.connection.commit()
        return len(rows)

    def ensure_scan_years(self, years: Iterable[int]) -> None:
        for year in years:
            self.connection.execute(
                """
                INSERT INTO acquisition_issuer_scans(
                    source_id, source_issuer_id, filing_year, status
                )
                SELECT source_id, source_issuer_id, %s, 'pending'
                FROM acquisition_issuers
                WHERE source_id = %s
                ON CONFLICT(source_id, source_issuer_id, filing_year) DO NOTHING
                """,
                (year, self.source_id),
            )
        self.connection.commit()

    def claim_issuers(
        self,
        limit: int,
        filing_year: int = 2026,
        rescan_hours: int | None = None,
    ) -> list[AcquisitionIssuer]:
        now = utc_now()
        rescan_before = (
            rescan_window_start(now, rescan_hours)
            if rescan_hours is not None
            else now
        )
        with self.connection.transaction():
            rows = self.connection.execute(
                """
                SELECT i.source_issuer_id AS cik, i.ticker, i.company_name,
                       i.exchange, i.priority
                FROM acquisition_issuer_scans ys
                JOIN acquisition_issuers i
                  ON i.source_id = ys.source_id
                 AND i.source_issuer_id = ys.source_issuer_id
                WHERE ys.source_id = %s
                  AND ys.filing_year = %s
                  AND (
                    (ys.status IN ('pending', 'retry') AND (
                      ys.next_attempt_at IS NULL OR ys.next_attempt_at <= %s
                    ))
                    OR (%s::integer IS NOT NULL AND ys.status = 'complete' AND ys.scanned_at <= %s)
                    OR ys.status = 'running'
                  )
                ORDER BY
                  CASE ys.status
                    WHEN 'running' THEN 0 WHEN 'retry' THEN 1
                    WHEN 'pending' THEN 2 ELSE 3
                  END,
                  i.priority,
                  ys.scanned_at NULLS FIRST,
                  i.ticker
                FOR UPDATE OF ys SKIP LOCKED
                LIMIT %s
                """,
                (self.source_id, filing_year, now, rescan_hours, rescan_before, limit),
            ).fetchall()
            if rows:
                with self.connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        UPDATE acquisition_issuer_scans
                        SET status = 'running', attempts = attempts + 1, claimed_at = now()
                        WHERE source_id = %s AND source_issuer_id = %s AND filing_year = %s
                        """,
                        [(self.source_id, row["cik"], filing_year) for row in rows],
                    )
        return [AcquisitionIssuer(**row) for row in rows]

    def complete_issuer(self, cik: str, filing_year: int = 2026) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_issuer_scans
            SET status = 'complete', scanned_at = now(), claimed_at = NULL,
                next_attempt_at = NULL, last_error = NULL
            WHERE source_id = %s AND source_issuer_id = %s AND filing_year = %s
            """,
            (self.source_id, cik, filing_year),
        )
        self.connection.commit()

    def fail_issuer(
        self,
        cik: str,
        error: str,
        filing_year: int = 2026,
        retry_minutes: int = 30,
    ) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_issuer_scans
            SET status = 'retry', claimed_at = NULL,
                next_attempt_at = now() + (%s * interval '1 minute'),
                last_error = %s
            WHERE source_id = %s AND source_issuer_id = %s AND filing_year = %s
            """,
            (retry_minutes, error[:1000], self.source_id, cik, filing_year),
        )
        self.connection.commit()

    def year_progress(self, filing_year: int) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, COUNT(*)::int AS count
            FROM acquisition_issuer_scans
            WHERE source_id = %s AND filing_year = %s
            GROUP BY status ORDER BY status
            """,
            (self.source_id, filing_year),
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
        cutoff = rescan_window_start(utc_now(), rescan_hours)
        row = self.connection.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM acquisition_issuer_scans
              WHERE source_id = %s AND filing_year = %s AND (
                status IN ('pending', 'running')
                OR (status = 'retry' AND (
                  next_attempt_at IS NULL OR next_attempt_at <= now()
                ))
                OR (status = 'complete' AND (
                  scanned_at IS NULL
                  OR scanned_at < %s
                ))
              )
            ) AS due
            """,
            (self.source_id, filing_year, cutoff),
        ).fetchone()
        return bool(row["due"])

    def complete_filing_ids(self, filing_ids: Iterable[str]) -> set[str]:
        identifiers = list(filing_ids)
        if not identifiers:
            return set()
        rows = self.connection.execute(
            """
            SELECT source_filing_id FROM acquisition_filings
            WHERE source_id = %s AND status = 'complete'
              AND source_filing_id = ANY(%s)
            """,
            (self.source_id, identifiers),
        ).fetchall()
        return {row["source_filing_id"] for row in rows}

    def upsert_filing(self, filing: dict, local_dir: Path, status: str, error: str = "") -> None:
        self.connection.execute(
            """
            INSERT INTO acquisition_filings(
              source_id, source_filing_id, source_issuer_id, form_raw, filing_date,
              report_date, accepted_at, primary_document, archive_url, local_dir,
              status, last_error, metadata
            ) VALUES (%s, %s, %s, %s, %s::date, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(source_id, source_filing_id) DO UPDATE SET
              source_issuer_id = EXCLUDED.source_issuer_id,
              primary_document = EXCLUDED.primary_document,
              local_dir = EXCLUDED.local_dir,
              status = EXCLUDED.status,
              last_error = EXCLUDED.last_error,
              metadata = EXCLUDED.metadata
            """,
            (
                self.source_id,
                filing["accession_number"],
                filing["cik"],
                filing["form"],
                filing["filing_date"],
                filing.get("report_date", ""),
                filing.get("accepted_at", ""),
                filing.get("primary_document", ""),
                filing["archive_url"],
                str(local_dir),
                status,
                error[:1000],
                Jsonb(filing),
            ),
        )
        self.connection.commit()

    def upsert_document(self, document: dict) -> None:
        self.connection.execute(
            """
            INSERT INTO acquisition_documents(
              source_id, source_filing_id, document_kind, source_url, local_path,
              content_type, byte_size, sha256, retrieved_at, status, last_error, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                      NULLIF(%s, '')::timestamptz, %s, %s, %s)
            ON CONFLICT(source_id, source_url) DO UPDATE SET
              source_filing_id = EXCLUDED.source_filing_id,
              local_path = EXCLUDED.local_path,
              content_type = EXCLUDED.content_type,
              byte_size = EXCLUDED.byte_size,
              sha256 = EXCLUDED.sha256,
              retrieved_at = EXCLUDED.retrieved_at,
              status = EXCLUDED.status,
              last_error = EXCLUDED.last_error,
              metadata = EXCLUDED.metadata
            """,
            (
                self.source_id,
                document["accession_number"],
                document["document_kind"],
                document["source_url"],
                document["local_path"],
                document.get("content_type", ""),
                document.get("byte_size"),
                document.get("sha256", ""),
                document.get("retrieved_at", ""),
                document["status"],
                document.get("last_error", "")[:1000],
                Jsonb({"cached": bool(document.get("cached", False))}),
            ),
        )
        self.connection.commit()

    def begin_run(self, run_id: str, target_year: int, mode: str) -> None:
        # Each source has one long-running worker service. Any open predecessor
        # therefore belongs to an interrupted batch, including recent deploys.
        self.connection.execute(
            """
            UPDATE acquisition_runs
            SET completed_at = now(), status = 'interrupted'
            WHERE source_id = %s AND status = 'running'
            """,
            (self.source_id,),
        )
        self.connection.execute(
            """
            INSERT INTO acquisition_runs(run_id, source_id, target_year, mode, status)
            VALUES (%s, %s, %s, %s, 'running')
            """,
            (run_id, self.source_id, target_year, mode),
        )
        self.connection.commit()

    def finish_run(self, run_id: str, status: str, counts: dict[str, int]) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_runs
            SET completed_at = now(), status = %s, issuer_count = %s,
                filing_count = %s, document_count = %s, error_count = %s
            WHERE run_id = %s
            """,
            (
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
        year_rows = self.connection.execute(
            """
            SELECT filing_year, status, COUNT(*)::int AS count
            FROM acquisition_issuer_scans
            WHERE source_id = %s
            GROUP BY filing_year, status
            ORDER BY filing_year DESC, status
            """,
            (self.source_id,),
        ).fetchall()
        years: dict[str, dict[str, int]] = {}
        for row in year_rows:
            years.setdefault(str(row["filing_year"]), {})[row["status"]] = row["count"]
        totals = self.connection.execute(
            """
            SELECT
              (SELECT COUNT(*)::int FROM acquisition_issuers WHERE source_id = %s) AS issuers,
              (SELECT COUNT(*)::int FROM acquisition_filings WHERE source_id = %s) AS filings,
              (SELECT COUNT(*)::int FROM acquisition_documents
                 WHERE source_id = %s AND status = 'complete') AS documents,
              (SELECT COALESCE(SUM(byte_size), 0)::bigint FROM acquisition_documents
                 WHERE source_id = %s AND status = 'complete') AS bytes
            """,
            (self.source_id, self.source_id, self.source_id, self.source_id),
        ).fetchone()
        latest_run = self.connection.execute(
            """
            SELECT run_id, target_year, mode, started_at, completed_at, status,
                   issuer_count, filing_count, document_count, error_count
            FROM acquisition_runs
            WHERE source_id = %s
            ORDER BY started_at DESC LIMIT 1
            """,
            (self.source_id,),
        ).fetchone()
        return {
            "issuers": totals["issuers"],
            "issuer_years": years,
            "filings": totals["filings"],
            "documents": totals["documents"],
            "bytes": totals["bytes"],
            "latest_run": serialize_datetimes(latest_run) if latest_run else None,
        }

    def import_sqlite(self, sqlite_path: Path, years: tuple[int, ...]) -> dict[str, int]:
        sqlite_connection = sqlite3.connect(sqlite_path)
        sqlite_connection.row_factory = sqlite3.Row
        try:
            issuer_rows = sqlite_connection.execute(
                "SELECT cik, ticker, company_name, exchange, priority FROM issuers"
            ).fetchall()
            self.upsert_issuers(AcquisitionIssuer(**dict(row)) for row in issuer_rows)
            self.ensure_scan_years(years)
            tables = {
                row["name"]
                for row in sqlite_connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "issuer_year_scans" in tables:
                scan_rows = sqlite_connection.execute("SELECT * FROM issuer_year_scans").fetchall()
            else:
                scan_rows = sqlite_connection.execute(
                    """
                    SELECT cik, 2026 AS filing_year, status, attempts,
                           next_attempt_at, scanned_at, last_error
                    FROM issuers
                    """
                ).fetchall()
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    """
                    UPDATE acquisition_issuer_scans
                    SET status = %s, attempts = %s,
                        next_attempt_at = NULLIF(%s, '')::timestamptz,
                        scanned_at = NULLIF(%s, '')::timestamptz,
                        last_error = %s
                    WHERE source_id = %s AND source_issuer_id = %s AND filing_year = %s
                    """,
                    [
                        (
                            row["status"],
                            row["attempts"],
                            row["next_attempt_at"] or "",
                            row["scanned_at"] or "",
                            row["last_error"],
                            self.source_id,
                            row["cik"],
                            row["filing_year"],
                        )
                        for row in scan_rows
                    ],
                )
            filing_rows = sqlite_connection.execute("SELECT * FROM filings").fetchall()
            for row in filing_rows:
                filing = {
                    "accession_number": row["accession_number"],
                    "cik": row["cik"],
                    "form": row["form"],
                    "filing_date": row["filing_date"],
                    "report_date": row["report_date"] or "",
                    "accepted_at": row["accepted_at"] or "",
                    "primary_document": row["primary_document"] or "",
                    "archive_url": row["archive_url"],
                }
                self.upsert_filing(
                    filing,
                    Path(row["local_dir"]),
                    row["status"],
                    row["last_error"] or "",
                )
            document_rows = sqlite_connection.execute("SELECT * FROM documents").fetchall()
            for row in document_rows:
                self.upsert_document(
                    {
                        "source_url": row["source_url"],
                        "accession_number": row["accession_number"],
                        "document_kind": row["document_kind"],
                        "local_path": row["local_path"],
                        "content_type": row["content_type"] or "",
                        "byte_size": row["byte_size"],
                        "sha256": row["sha256"] or "",
                        "retrieved_at": row["retrieved_at"] or "",
                        "status": row["status"],
                        "last_error": row["last_error"] or "",
                    }
                )
            self.connection.commit()
            return {
                "issuers": len(issuer_rows),
                "scans": len(scan_rows),
                "filings": len(filing_rows),
                "documents": len(document_rows),
            }
        finally:
            sqlite_connection.close()


def serialize_datetimes(row: dict) -> dict:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }
