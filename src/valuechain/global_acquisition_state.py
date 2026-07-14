from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gcu.models import EntityRef, FilingRef, SourceDefinition
from valuechain.acquisition_schema import ensure_acquisition_schema


def filing_local_dir(
    raw_root: Path,
    source_id: str,
    filing_year: int,
    source_entity_id: str,
    filing_id: str,
) -> Path:
    """Return the canonical on-disk directory recorded in acquisition metadata."""
    safe_filing_id = filing_id.replace("/", "_").replace("\\", "_")
    if source_id == "cninfo":
        safe_entity_id = source_entity_id.replace("/", "_").replace("\\", "_")
        return (
            raw_root
            / source_id
            / str(filing_year)
            / safe_entity_id
            / safe_filing_id
        )
    return raw_root / source_id / str(filing_year) / safe_filing_id


class GlobalSourceAcquisitionState:
    """PostgreSQL queue and provenance state for non-SEC source downloaders."""

    def __init__(
        self,
        database_url: str,
        source_id: str,
        ensure_schema: bool = True,
    ) -> None:
        self.source_id = source_id
        self.connection = psycopg.connect(database_url, row_factory=dict_row)
        if ensure_schema:
            ensure_acquisition_schema(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> GlobalSourceAcquisitionState:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def ensure_source(self, source: SourceDefinition) -> None:
        self.connection.execute(
            """
            INSERT INTO acquisition_sources(source_id, authority, canonical, enabled, config)
            VALUES (%s, %s, true, true, %s)
            ON CONFLICT(source_id) DO UPDATE SET
              authority = EXCLUDED.authority,
              enabled = true,
              config = acquisition_sources.config || EXCLUDED.config,
              updated_at = now()
            """,
            (
                source.source_id,
                source.name,
                Jsonb(source.model_dump(mode="json", exclude_none=True)),
            ),
        )
        self.connection.commit()

    def upsert_entities(self, entities: Iterable[EntityRef], priority: int = 500) -> int:
        rows = list(entities)
        if not rows:
            return 0
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO acquisition_issuers(
                  source_id, source_issuer_id, ticker, company_name, exchange, priority, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(source_id, source_issuer_id) DO UPDATE SET
                  ticker = EXCLUDED.ticker,
                  company_name = EXCLUDED.company_name,
                  exchange = EXCLUDED.exchange,
                  priority = LEAST(acquisition_issuers.priority, EXCLUDED.priority),
                  metadata = acquisition_issuers.metadata || EXCLUDED.metadata,
                  updated_at = now()
                """,
                [
                    (
                        self.source_id,
                        row.source_entity_id,
                        row.ticker or "",
                        row.legal_name,
                        row.exchange or "",
                        priority,
                        Jsonb(
                            {
                                **row.metadata,
                                "entity_id": row.entity_id,
                                "jurisdiction": row.jurisdiction,
                                "lei": row.lei,
                                "aliases": row.aliases,
                            }
                        ),
                    )
                    for row in rows
                ],
            )
        self.connection.commit()
        return len(rows)

    def record_universe_snapshot(
        self,
        *,
        path: Path,
        source_url: str,
        row_count: int,
        sha256: str,
        retrieved_at: datetime,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO acquisition_universe_snapshots(
              source_id, source_url, local_path, sha256, row_count, retrieved_at,
              metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(source_id, sha256) DO UPDATE SET
              local_path = EXCLUDED.local_path,
              row_count = EXCLUDED.row_count,
              retrieved_at = EXCLUDED.retrieved_at,
              metadata = acquisition_universe_snapshots.metadata || EXCLUDED.metadata
            """,
            (
                self.source_id,
                source_url,
                str(path),
                sha256,
                row_count,
                retrieved_at,
                Jsonb({"listener_refresh": True}),
            ),
        )
        self.connection.commit()

    def checkpoint_due(self, checkpoint_key: str, max_age_hours: int) -> bool:
        row = self.connection.execute(
            """
            SELECT status, started_at, completed_at, next_attempt_at
            FROM acquisition_source_checkpoints
            WHERE source_id = %s AND checkpoint_key = %s
            """,
            (self.source_id, checkpoint_key),
        ).fetchone()
        if row is None:
            return True
        now = datetime.now(UTC)
        if row["status"] == "retry" and row["next_attempt_at"] is not None:
            return row["next_attempt_at"] <= now
        if row["status"] == "running" and row["started_at"] is not None:
            return row["started_at"] <= now - timedelta(hours=2)
        if row["status"] != "complete" or row["completed_at"] is None:
            return True
        return row["completed_at"] <= now - timedelta(hours=max_age_hours)

    def begin_checkpoint(self, checkpoint_key: str, metadata: dict[str, Any] | None = None) -> None:
        self.connection.execute(
            """
            INSERT INTO acquisition_source_checkpoints(
              source_id, checkpoint_key, status, attempts, started_at, metadata
            ) VALUES (%s, %s, 'running', 1, now(), %s)
            ON CONFLICT(source_id, checkpoint_key) DO UPDATE SET
              status = 'running', attempts = acquisition_source_checkpoints.attempts + 1,
              started_at = now(), next_attempt_at = NULL, last_error = NULL,
              metadata = acquisition_source_checkpoints.metadata || EXCLUDED.metadata
            """,
            (self.source_id, checkpoint_key, Jsonb(metadata or {})),
        )
        self.connection.commit()

    def complete_checkpoint(
        self, checkpoint_key: str, metadata: dict[str, Any] | None = None
    ) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_source_checkpoints
            SET status = 'complete', completed_at = now(), next_attempt_at = NULL,
                last_error = NULL, metadata = metadata || %s
            WHERE source_id = %s AND checkpoint_key = %s
            """,
            (Jsonb(metadata or {}), self.source_id, checkpoint_key),
        )
        self.connection.commit()

    def fail_checkpoint(self, checkpoint_key: str, error: str, retry_minutes: int = 30) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_source_checkpoints
            SET status = 'retry', last_error = %s,
                next_attempt_at = now() + (%s * interval '1 minute')
            WHERE source_id = %s AND checkpoint_key = %s
            """,
            (error[:1000], retry_minutes, self.source_id, checkpoint_key),
        )
        self.connection.commit()

    def upsert_filings(self, filings: Iterable[FilingRef], raw_root: Path) -> int:
        rows = [row for row in filings if row.source_entity_id and row.filed_at]
        if not rows:
            return 0
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO acquisition_filings(
                  source_id, source_filing_id, source_issuer_id, form_raw, filing_date,
                  report_date, accepted_at, primary_document, archive_url, local_dir,
                  status, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, '', %s, %s, %s, 'discovered', %s)
                ON CONFLICT(source_id, source_filing_id) DO UPDATE SET
                  source_issuer_id = EXCLUDED.source_issuer_id,
                  form_raw = EXCLUDED.form_raw,
                  filing_date = EXCLUDED.filing_date,
                  report_date = EXCLUDED.report_date,
                  primary_document = EXCLUDED.primary_document,
                  archive_url = EXCLUDED.archive_url,
                  local_dir = EXCLUDED.local_dir,
                  status = CASE WHEN acquisition_filings.status = 'complete'
                                THEN 'complete' ELSE EXCLUDED.status END,
                  metadata = acquisition_filings.metadata || EXCLUDED.metadata
                """,
                [self._filing_values(row, raw_root) for row in rows],
            )
        self.connection.commit()
        return len(rows)

    def _filing_values(self, filing: FilingRef, raw_root: Path) -> tuple[Any, ...]:
        year = filing.filed_at.year
        local_dir = filing_local_dir(
            raw_root,
            self.source_id,
            year,
            filing.source_entity_id,
            filing.filing_id,
        )
        primary_name = (
            filing.primary_document_url.split("?", 1)[0].rsplit("/", 1)[-1]
            if filing.primary_document_url
            else ""
        )
        return (
            self.source_id,
            filing.filing_id,
            filing.source_entity_id,
            filing.form or "unknown",
            filing.filed_at,
            filing.period_end.isoformat() if filing.period_end else "",
            primary_name,
            filing.detail_url or filing.primary_document_url or "",
            str(local_dir),
            Jsonb(
                {
                    **filing.metadata,
                    "entity_id": filing.entity_id,
                    "title": filing.title,
                    "language": filing.language,
                    "primary_document_url": filing.primary_document_url,
                }
            ),
        )

    def claim_filings(self, filing_year: int, limit: int) -> list[dict[str, Any]]:
        with self.connection.transaction():
            rows = self.connection.execute(
                """
                SELECT * FROM acquisition_filings
                WHERE source_id = %s
                  AND EXTRACT(YEAR FROM filing_date) = %s
                  AND status IN ('discovered', 'retry')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                ORDER BY filing_date, source_filing_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (self.source_id, filing_year, limit),
            ).fetchall()
            if rows:
                with self.connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        UPDATE acquisition_filings
                        SET status = 'downloading', last_error = NULL
                        WHERE source_id = %s AND source_filing_id = %s
                        """,
                        [(self.source_id, row["source_filing_id"]) for row in rows],
                    )
        return [dict(row) for row in rows]

    def recover_downloading_filings(self, error: str) -> int:
        """Return filings left in downloading state by an interrupted worker to retry."""
        cursor = self.connection.execute(
            """
            UPDATE acquisition_filings
            SET status = 'retry', last_error = %s, next_attempt_at = now()
            WHERE source_id = %s AND status = 'downloading'
            """,
            (error[:1000], self.source_id),
        )
        self.connection.commit()
        return cursor.rowcount

    def complete_filing(self, filing_id: str) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_filings
            SET status = 'complete', last_error = NULL, next_attempt_at = NULL
            WHERE source_id = %s AND source_filing_id = %s
            """,
            (self.source_id, filing_id),
        )
        self.connection.commit()

    def fail_filing(self, filing_id: str, error: str, retry_minutes: int = 30) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_filings
            SET status = 'retry', last_error = %s,
                next_attempt_at = now() + (%s * interval '1 minute')
            WHERE source_id = %s AND source_filing_id = %s
            """,
            (error[:1000], retry_minutes, self.source_id, filing_id),
        )
        self.connection.commit()

    def upsert_document(self, filing_id: str, kind: str, document: dict[str, Any]) -> None:
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
              metadata = acquisition_documents.metadata || EXCLUDED.metadata
            """,
            (
                self.source_id,
                filing_id,
                kind,
                document["source_url"],
                document["local_path"],
                document.get("content_type", ""),
                document.get("byte_size"),
                document.get("sha256", ""),
                document.get("retrieved_at", ""),
                document.get("status", "complete"),
                document.get("last_error", "")[:1000],
                Jsonb(document.get("metadata", {})),
            ),
        )
        self.connection.commit()

    def upsert_source_object(self, object_key: str, object_type: str, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO acquisition_source_objects(
              source_id, object_key, object_type, source_url, local_path, content_type,
              byte_size, sha256, retrieved_at, status, last_error, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                      NULLIF(%s, '')::timestamptz, %s, %s, %s)
            ON CONFLICT(source_id, object_key) DO UPDATE SET
              source_url = EXCLUDED.source_url, local_path = EXCLUDED.local_path,
              content_type = EXCLUDED.content_type, byte_size = EXCLUDED.byte_size,
              sha256 = EXCLUDED.sha256, retrieved_at = EXCLUDED.retrieved_at,
              status = EXCLUDED.status, last_error = EXCLUDED.last_error,
              metadata = acquisition_source_objects.metadata || EXCLUDED.metadata
            """,
            (
                self.source_id,
                object_key,
                object_type,
                row["source_url"],
                row["local_path"],
                row.get("content_type", ""),
                row.get("byte_size"),
                row.get("sha256", ""),
                row.get("retrieved_at", ""),
                row.get("status", "complete"),
                row.get("last_error", "")[:1000],
                Jsonb(row.get("metadata", {})),
            ),
        )
        self.connection.commit()

    def stats(self) -> dict[str, Any]:
        totals = self.connection.execute(
            """
            SELECT
              (SELECT COUNT(*)::int FROM acquisition_issuers WHERE source_id = %s) issuers,
              (SELECT COUNT(*)::int FROM acquisition_filings WHERE source_id = %s) filings,
              (SELECT COUNT(*)::int FROM acquisition_documents
                 WHERE source_id = %s AND status = 'complete') documents,
              (SELECT COALESCE(SUM(byte_size), 0)::bigint FROM acquisition_documents
                 WHERE source_id = %s AND status = 'complete') bytes,
              (SELECT COUNT(*)::int FROM acquisition_source_objects
                 WHERE source_id = %s AND status = 'complete') source_objects,
              (SELECT COALESCE(SUM(byte_size), 0)::bigint FROM acquisition_source_objects
                 WHERE source_id = %s AND status = 'complete') source_object_bytes
            """,
            (self.source_id,) * 6,
        ).fetchone()
        statuses = self.connection.execute(
            """
            SELECT status, COUNT(*)::int count FROM acquisition_filings
            WHERE source_id = %s GROUP BY status ORDER BY status
            """,
            (self.source_id,),
        ).fetchall()
        return {
            **dict(totals),
            "filing_statuses": {row["status"]: row["count"] for row in statuses},
        }
