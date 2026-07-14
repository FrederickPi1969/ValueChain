from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from valuechain.acquisition_schema import ensure_acquisition_schema
from valuechain.disclosure_resolver import ResolveDisclosureRequest, request_key


TERMINAL_STATUSES = {"complete", "not_found", "failed", "unsupported"}


class AdHocRequestState:
    def __init__(self, database_url: str, ensure_schema: bool = True) -> None:
        self.connection = psycopg.connect(database_url, row_factory=dict_row)
        if ensure_schema:
            ensure_acquisition_schema(self.connection)

    def __enter__(self) -> "AdHocRequestState":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def enqueue(
        self,
        request: ResolveDisclosureRequest,
        source_id: str,
        source_issuer_id: str,
    ) -> dict[str, Any]:
        key = request_key(request, source_id, source_issuer_id)
        request_id = uuid4()
        payload = request.model_dump(mode="json")
        row = self.connection.execute(
            """
            INSERT INTO acquisition_ad_hoc_requests(
              request_id, request_key, source_id, source_issuer_id,
              requested_year, canonical_document_type, source_document_type,
              year_basis, include_amendments, request_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(request_key) DO UPDATE SET
              status = CASE
                WHEN acquisition_ad_hoc_requests.status IN ('failed', 'not_found')
                  AND acquisition_ad_hoc_requests.updated_at < now() - interval '6 hours'
                THEN 'queued'
                ELSE acquisition_ad_hoc_requests.status
              END,
              next_attempt_at = CASE
                WHEN acquisition_ad_hoc_requests.status IN ('failed', 'not_found')
                  AND acquisition_ad_hoc_requests.updated_at < now() - interval '6 hours'
                THEN NULL
                ELSE acquisition_ad_hoc_requests.next_attempt_at
              END,
              error_code = CASE
                WHEN acquisition_ad_hoc_requests.status IN ('failed', 'not_found')
                  AND acquisition_ad_hoc_requests.updated_at < now() - interval '6 hours'
                THEN NULL
                ELSE acquisition_ad_hoc_requests.error_code
              END,
              error_message = CASE
                WHEN acquisition_ad_hoc_requests.status IN ('failed', 'not_found')
                  AND acquisition_ad_hoc_requests.updated_at < now() - interval '6 hours'
                THEN NULL
                ELSE acquisition_ad_hoc_requests.error_message
              END,
              request_payload = EXCLUDED.request_payload,
              updated_at = now()
            RETURNING *
            """,
            (
                request_id,
                key,
                source_id,
                source_issuer_id,
                request.year,
                str(request.document_type),
                request.source_document_type or "",
                str(request.year_basis),
                request.include_amendments,
                Jsonb(payload),
            ),
        ).fetchone()
        self.connection.commit()
        return dict(row)

    def get(self, request_id: UUID | str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM acquisition_ad_hoc_requests WHERE request_id = %s",
            (request_id,),
        ).fetchone()
        return dict(row) if row else None

    def claim(self) -> dict[str, Any] | None:
        with self.connection.transaction():
            row = self.connection.execute(
                """
                SELECT * FROM acquisition_ad_hoc_requests
                WHERE status IN ('queued', 'retry')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            self.connection.execute(
                """
                UPDATE acquisition_ad_hoc_requests
                SET status = 'discovering', attempts = attempts + 1,
                    claimed_at = now(), updated_at = now(), error_code = NULL,
                    error_message = NULL
                WHERE request_id = %s
                """,
                (row["request_id"],),
            )
        claimed = dict(row)
        claimed["status"] = "discovering"
        claimed["attempts"] = int(claimed["attempts"]) + 1
        return claimed

    def mark_downloading(self, request_id: UUID | str) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_ad_hoc_requests
            SET status = 'downloading', updated_at = now()
            WHERE request_id = %s
            """,
            (request_id,),
        )
        self.connection.commit()

    def complete(
        self,
        request_id: UUID | str,
        document_ids: list[int],
        result: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_ad_hoc_requests
            SET status = 'complete', result_document_ids = %s, result = %s,
                completed_at = now(), claimed_at = NULL, next_attempt_at = NULL,
                updated_at = now(), error_code = NULL, error_message = NULL
            WHERE request_id = %s
            """,
            (document_ids, Jsonb(result), request_id),
        )
        self.connection.commit()

    def finish_without_result(
        self,
        request_id: UUID | str,
        status: str,
        error_code: str,
        error_message: str,
    ) -> None:
        if status not in {"not_found", "failed", "unsupported"}:
            raise ValueError(f"Invalid terminal request status: {status}")
        self.connection.execute(
            """
            UPDATE acquisition_ad_hoc_requests
            SET status = %s, error_code = %s, error_message = %s,
                completed_at = now(), claimed_at = NULL, next_attempt_at = NULL,
                updated_at = now()
            WHERE request_id = %s
            """,
            (status, error_code, error_message[:2000], request_id),
        )
        self.connection.commit()

    def retry(
        self,
        request_id: UUID | str,
        error_code: str,
        error_message: str,
        delay_seconds: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE acquisition_ad_hoc_requests
            SET status = 'retry', error_code = %s, error_message = %s,
                claimed_at = NULL,
                next_attempt_at = now() + (%s * interval '1 second'),
                updated_at = now()
            WHERE request_id = %s
            """,
            (error_code, error_message[:2000], delay_seconds, request_id),
        )
        self.connection.commit()

    def recover_stale(self, age_minutes: int = 30) -> int:
        cursor = self.connection.execute(
            """
            UPDATE acquisition_ad_hoc_requests
            SET status = 'retry', claimed_at = NULL, next_attempt_at = now(),
                error_code = 'worker_interrupted',
                error_message = 'Recovered a request left active by an interrupted worker',
                updated_at = now()
            WHERE status IN ('discovering', 'downloading')
              AND claimed_at < now() - (%s * interval '1 minute')
            """,
            (age_minutes,),
        )
        self.connection.commit()
        return cursor.rowcount


def serialize_request_row(row: dict[str, Any]) -> dict[str, Any]:
    hidden = {"request_key"}
    output: dict[str, Any] = {}
    for key, value in row.items():
        if key in hidden:
            continue
        if isinstance(value, (datetime,)):
            output[key] = value.astimezone(UTC).isoformat()
        elif isinstance(value, UUID):
            output[key] = str(value)
        else:
            output[key] = value
    return output
