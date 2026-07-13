from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gcu.models import EntityRef, FilingRef, SourceDefinition
from valuechain.acquisition_schema import ensure_acquisition_schema



def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def deduplicate_entities(entities: Iterable[EntityRef]) -> list[EntityRef]:
    """Collapse exact source-key duplicates and reject ambiguous key collisions."""
    unique: dict[tuple[str, str], EntityRef] = {}
    for entity in entities:
        key = (entity.source_id, entity.source_entity_id)
        previous = unique.get(key)
        if previous is None:
            unique[key] = entity.model_copy(deep=True)
            continue
        identity = (entity.legal_name, entity.ticker, entity.exchange)
        previous_identity = (previous.legal_name, previous.ticker, previous.exchange)
        if identity != previous_identity:
            raise ValueError(
                f"Conflicting entities share source key {key!r}: "
                f"{previous_identity!r} != {identity!r}"
            )
        previous.metadata["duplicate_source_rows"] = (
            int(previous.metadata.get("duplicate_source_rows", 1)) + 1
        )
        previous.aliases = sorted(set(previous.aliases) | set(entity.aliases))
    return list(unique.values())


def csv_data_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def read_entity_csv(path: Path, source_id: str | None = None) -> list[EntityRef]:
    entities: list[EntityRef] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            row_source = (source_id or row.get("source_id") or "").strip()
            source_entity_id = (row.get("source_entity_id") or row.get("entity_id") or "").strip()
            legal_name = (row.get("legal_name") or row.get("company_name") or "").strip()
            if not row_source or not source_entity_id or not legal_name:
                continue
            metadata = _json_value(row.get("metadata"), {})
            metadata["snapshot_row"] = {
                key: value
                for key, value in row.items()
                if key not in {"metadata", "aliases"} and value not in (None, "")
            }
            entities.append(
                EntityRef(
                    entity_id=(row.get("entity_id") or f"{row_source}-{source_entity_id}").strip(),
                    source_id=row_source,
                    source_entity_id=source_entity_id,
                    legal_name=legal_name,
                    jurisdiction=(row.get("jurisdiction") or None),
                    exchange=(row.get("exchange") or None),
                    ticker=(row.get("ticker") or None),
                    lei=(row.get("lei") or None),
                    isin=(row.get("isin") or None),
                    local_registry_id=(row.get("local_registry_id") or None),
                    aliases=_json_value(row.get("aliases"), []),
                    metadata=metadata,
                )
            )
    return deduplicate_entities(entities)


def read_filing_jsonl(path: Path, source_id: str | None = None) -> list[FilingRef]:
    filings: list[FilingRef] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            payload = json.loads(line)
            if source_id:
                payload["source_id"] = source_id
            filings.append(FilingRef.model_validate(payload))
    return filings


class GlobalUniverseStore:
    """PostgreSQL bridge for source-local global issuers and disclosure metadata."""

    def __init__(self, database_url: str) -> None:
        self.connection = psycopg.connect(database_url, row_factory=dict_row)
        ensure_acquisition_schema(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> GlobalUniverseStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def upsert_source(
        self,
        source_id: str,
        *,
        authority: str = "",
        canonical: bool = True,
        enabled: bool = False,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO acquisition_sources(source_id, authority, canonical, enabled, config)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(source_id) DO UPDATE SET
              authority = COALESCE(NULLIF(EXCLUDED.authority, ''), acquisition_sources.authority),
              canonical = acquisition_sources.canonical OR EXCLUDED.canonical,
              enabled = acquisition_sources.enabled OR EXCLUDED.enabled,
              config = acquisition_sources.config || EXCLUDED.config,
              updated_at = now()
            """,
            (source_id, authority, canonical, enabled, Jsonb(config or {})),
        )
        self.connection.commit()

    def upsert_source_definition(self, source: SourceDefinition, enabled: bool = False) -> None:
        payload = source.model_dump(mode="json", exclude_none=True)
        self.upsert_source(
            source.source_id,
            authority=source.name,
            canonical=True,
            enabled=enabled,
            config=payload,
        )

    def upsert_entities(self, entities: Iterable[EntityRef], priority: int = 500) -> int:
        rows = deduplicate_entities(entities)
        if not rows:
            return 0
        source_ids = {row.source_id for row in rows}
        if len(source_ids) != 1:
            raise ValueError("One universe import must contain exactly one source_id")
        source_id = next(iter(source_ids))
        self.upsert_source(source_id)
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
                        row.source_id,
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
                                "isin": row.isin,
                                "local_registry_id": row.local_registry_id,
                                "aliases": row.aliases,
                            }
                        ),
                    )
                    for row in rows
                ],
            )
        self.connection.commit()
        return len(rows)

    def upsert_filings(self, filings: Iterable[FilingRef]) -> int:
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
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '', 'discovered', %s)
                ON CONFLICT(source_id, source_filing_id) DO UPDATE SET
                  source_issuer_id = EXCLUDED.source_issuer_id,
                  form_raw = EXCLUDED.form_raw,
                  filing_date = EXCLUDED.filing_date,
                  report_date = EXCLUDED.report_date,
                  primary_document = EXCLUDED.primary_document,
                  archive_url = EXCLUDED.archive_url,
                  metadata = acquisition_filings.metadata || EXCLUDED.metadata
                """,
                [
                    (
                        row.source_id,
                        row.filing_id,
                        row.source_entity_id,
                        row.form or "unknown",
                        row.filed_at,
                        row.period_end.isoformat() if row.period_end else None,
                        None,
                        row.primary_document_url,
                        row.detail_url or row.primary_document_url or "",
                        Jsonb(
                            {
                                **row.metadata,
                                "entity_id": row.entity_id,
                                "title": row.title,
                                "language": row.language,
                                "amendment": row.amendment,
                            }
                        ),
                    )
                    for row in rows
                ],
            )
        self.connection.commit()
        return len(rows)

    def record_snapshot(
        self,
        source_id: str,
        path: Path,
        row_count: int,
        *,
        source_url: str = "",
        retrieved_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        digest = file_sha256(path)
        self.connection.execute(
            """
            INSERT INTO acquisition_universe_snapshots(
              source_id, source_url, local_path, sha256, row_count, retrieved_at, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(source_id, sha256) DO UPDATE SET
              local_path = EXCLUDED.local_path,
              row_count = EXCLUDED.row_count,
              metadata = acquisition_universe_snapshots.metadata || EXCLUDED.metadata
            """,
            (
                source_id,
                source_url,
                str(path),
                digest,
                row_count,
                retrieved_at,
                Jsonb(metadata or {}),
            ),
        )
        self.connection.commit()
        return digest

    def source_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT source_id, count(*)::int AS count
            FROM acquisition_issuers
            GROUP BY source_id
            ORDER BY source_id
            """
        ).fetchall()
        return {row["source_id"]: row["count"] for row in rows}
