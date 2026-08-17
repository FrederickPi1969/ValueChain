from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from valuechain.document_storage import (
    finalize_compression,
    prepare_document_record,
)

ADVISORY_LOCK_NAMESPACE = 0x5643


class CompressionWorker:
    def __init__(self, database_url: str) -> None:
        self.connection = psycopg.connect(database_url, row_factory=dict_row)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"

    def close(self) -> None:
        self.connection.close()

    def _candidate(
        self,
        minimum_age_minutes: int,
        *,
        source_id: str = "",
        max_bytes: int | None = None,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT document_id, source_id, source_filing_id, document_kind,
                   local_path, content_type, byte_size, sha256, status, metadata
            FROM acquisition_documents
            WHERE status = 'complete'
              AND retrieved_at < now() - (%s * interval '1 minute')
              AND local_path NOT LIKE '%%.zst'
              AND NOT coalesce(
                    (metadata->>'storage_compression_skipped')::boolean,
                    false
                  )
              AND NOT coalesce(
                    (metadata->>'storage_compression_missing')::boolean,
                    false
                  )
              AND NOT (metadata ? 'storage_compression_error')
              AND lower(local_path) ~ '\\.(txt|html?|xhtml|json|xml|xbrl|pdf)$'
              AND byte_size >= %s
              AND (%s = '' OR source_id = %s)
              AND (%s::bigint IS NULL OR byte_size <= %s)
            ORDER BY byte_size DESC, document_id
            LIMIT 100
            """,
            (
                minimum_age_minutes,
                int(os.getenv("VALUECHAIN_DOCUMENT_COMPRESSION_MIN_BYTES", "262144")),
                source_id,
                source_id,
                max_bytes,
                max_bytes,
            ),
        ).fetchall()
        for candidate in row:
            locked = self.connection.execute(
                "SELECT pg_try_advisory_lock(%s, %s) AS locked",
                (ADVISORY_LOCK_NAMESPACE, candidate["document_id"]),
            ).fetchone()["locked"]
            if not locked:
                continue
            fresh = self.connection.execute(
                """
                SELECT document_id, source_id, source_filing_id, document_kind,
                       local_path, content_type, byte_size, sha256, status, metadata
                FROM acquisition_documents
                WHERE document_id = %s AND status = 'complete'
                """,
                (candidate["document_id"],),
            ).fetchone()
            if fresh and fresh["local_path"] == candidate["local_path"]:
                # Session advisory locks survive commit; avoid an idle transaction
                # while zstd streams a large source file.
                self.connection.commit()
                return dict(fresh)
            self.connection.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (ADVISORY_LOCK_NAMESPACE, candidate["document_id"]),
            )
            self.connection.commit()
        self.connection.commit()
        return None

    def run_one(
        self,
        minimum_age_minutes: int = 10,
        *,
        source_id: str = "",
        max_bytes: int | None = None,
    ) -> dict[str, Any] | None:
        row = self._candidate(
            minimum_age_minutes,
            source_id=source_id,
            max_bytes=max_bytes,
        )
        if row is None:
            return None
        document_id = row["document_id"]
        original_path = str(row["local_path"])
        prepared = None
        try:
            if not Path(original_path).is_file():
                compressed = Path(f"{original_path}.zst")
                if not compressed.is_file():
                    self.connection.execute(
                        """
                        UPDATE acquisition_documents
                        SET metadata = metadata || %s
                        WHERE document_id = %s AND local_path = %s
                        """,
                        (
                            Jsonb(
                                {
                                    "storage_compression_missing": True,
                                    "storage_compression_worker": self.worker_id,
                                }
                            ),
                            document_id,
                            original_path,
                        ),
                    )
                    self.connection.commit()
                    return {"document_id": document_id, "status": "missing"}
            row["metadata"] = dict(row.get("metadata") or {})
            prepared = prepare_document_record(row, enabled=True)
            if prepared is None:
                self.connection.execute(
                    """
                    UPDATE acquisition_documents
                    SET metadata = metadata || %s
                    WHERE document_id = %s AND local_path = %s
                    """,
                    (
                        Jsonb({"storage_compression_skipped": True}),
                        document_id,
                        original_path,
                    ),
                )
                self.connection.commit()
                return {"document_id": document_id, "status": "skipped"}
            cursor = self.connection.execute(
                """
                UPDATE acquisition_documents
                SET local_path = %s, byte_size = %s, sha256 = %s,
                    metadata = %s
                WHERE document_id = %s AND local_path = %s AND status = 'complete'
                """,
                (
                    row["local_path"],
                    row["byte_size"],
                    row["sha256"],
                    Jsonb(row["metadata"]),
                    document_id,
                    original_path,
                ),
            )
            self.connection.commit()
            if cursor.rowcount != 1:
                if prepared.source_path.exists():
                    prepared.stored_path.unlink(missing_ok=True)
                return {"document_id": document_id, "status": "changed"}
            finalize_compression(prepared)
            return {
                "document_id": document_id,
                "source_id": row["source_id"],
                "status": "compressed",
                "original_bytes": prepared.original_size,
                "stored_bytes": prepared.stored_size,
                "saved_bytes": prepared.original_size - prepared.stored_size,
            }
        except Exception as exc:  # noqa: BLE001 - quarantine one bad corpus file
            self.connection.rollback()
            try:
                self.connection.execute(
                    """
                    UPDATE acquisition_documents
                    SET metadata = metadata || %s
                    WHERE document_id = %s AND local_path = %s
                    """,
                    (
                        Jsonb(
                            {
                                "storage_compression_error": (
                                    f"{type(exc).__name__}: {exc}"
                                )[:1000],
                                "storage_compression_failed_at": datetime.now(
                                    UTC
                                ).isoformat(),
                                "storage_compression_worker": self.worker_id,
                            }
                        ),
                        document_id,
                        original_path,
                    ),
                )
                self.connection.commit()
            except psycopg.Error:
                self.connection.rollback()
                raise
            return {
                "document_id": document_id,
                "source_id": row["source_id"],
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            }
        finally:
            self.connection.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (ADVISORY_LOCK_NAMESPACE, document_id),
            )
            self.connection.commit()


def database_url() -> str:
    value = os.getenv("VALUECHAIN_ACQUISITION_DATABASE_URL") or os.getenv(
        "VALUECHAIN_DATABASE_URL"
    )
    if not value:
        raise RuntimeError("VALUECHAIN_ACQUISITION_DATABASE_URL is required")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="valuechain-compress")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run-batch", "run-worker"):
        command = subparsers.add_parser(name)
        command.add_argument("--limit", type=int, default=100)
        command.add_argument("--minimum-age-minutes", type=int, default=10)
        command.add_argument("--idle-seconds", type=float, default=30.0)
        command.add_argument("--source-id", default="")
        command.add_argument("--max-bytes", type=int, default=None)
    return parser


def run_batch(
    worker: CompressionWorker,
    limit: int,
    minimum_age_minutes: int,
    *,
    source_id: str = "",
    max_bytes: int | None = None,
) -> dict[str, int]:
    counts = {
        "compressed": 0,
        "skipped": 0,
        "missing": 0,
        "changed": 0,
        "error": 0,
    }
    saved_bytes = 0
    for _ in range(limit):
        result = worker.run_one(
            minimum_age_minutes,
            source_id=source_id,
            max_bytes=max_bytes,
        )
        if result is None:
            break
        status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1
        saved_bytes += int(result.get("saved_bytes") or 0)
        print(json.dumps(result, sort_keys=True), flush=True)
    counts["saved_bytes"] = saved_bytes
    return counts


def main(argv: list[str] | None = None) -> None:
    def stop_cleanly(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_cleanly)
    signal.signal(signal.SIGINT, stop_cleanly)
    args = build_parser().parse_args(argv)
    worker = CompressionWorker(database_url())
    try:
        if args.command == "run-batch":
            print(
                json.dumps(
                    run_batch(
                        worker,
                        args.limit,
                        args.minimum_age_minutes,
                        source_id=args.source_id,
                        max_bytes=args.max_bytes,
                    ),
                    sort_keys=True,
                )
            )
            return
        while True:
            counts = run_batch(
                worker,
                args.limit,
                args.minimum_age_minutes,
                source_id=args.source_id,
                max_bytes=args.max_bytes,
            )
            if not counts["compressed"]:
                time.sleep(args.idle_seconds)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
