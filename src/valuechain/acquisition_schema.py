from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "acquisition_schema.sql"
SCHEMA_LOCK_ID = 8_641_969


def ensure_acquisition_schema(connection: psycopg.Connection) -> None:
    """Serialize idempotent DDL across independently scheduled source workers."""
    connection.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
    try:
        connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))
        connection.commit()


def prepare_acquisition_schema(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        ensure_acquisition_schema(connection)


class AcquisitionSchemaGuard:
    """Prepare acquisition DDL once before a long-running worker enters its hot path."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.prepared = False

    async def prepare(self) -> None:
        if self.prepared:
            return
        await asyncio.to_thread(prepare_acquisition_schema, self.database_url)
        self.prepared = True
