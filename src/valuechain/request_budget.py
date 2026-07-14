from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg


class RequestBudgetExceeded(RuntimeError):
    """Raised before a request that would exceed a source's daily allowance."""


@dataclass(frozen=True)
class RequestBudgetSnapshot:
    source_id: str
    usage_date: str
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


class PostgresDailyRequestBudget:
    """Atomically reserve API attempts across workers and process restarts."""

    def __init__(
        self,
        database_url: str,
        source_id: str,
        daily_limit: int,
        *,
        timezone: str = "UTC",
    ) -> None:
        if daily_limit < 1:
            raise ValueError("daily_limit must be positive")
        self.database_url = database_url
        self.source_id = source_id
        self.daily_limit = daily_limit
        self.timezone = ZoneInfo(timezone)
        self._connection: psycopg.Connection | None = None
        self._lock = threading.Lock()

    def _today(self) -> str:
        return datetime.now(self.timezone).date().isoformat()

    def _connect(self) -> psycopg.Connection:
        if self._connection is None or self._connection.closed:
            self._connection = psycopg.connect(self.database_url)
        return self._connection

    def reserve(self) -> RequestBudgetSnapshot:
        usage_date = self._today()
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                """
                INSERT INTO acquisition_api_usage(
                  source_id, usage_date, request_count, request_limit
                ) VALUES (%s, %s, 1, %s)
                ON CONFLICT(source_id, usage_date) DO UPDATE SET
                  request_count = acquisition_api_usage.request_count + 1,
                  request_limit = LEAST(
                    acquisition_api_usage.request_limit,
                    EXCLUDED.request_limit
                  ),
                  updated_at = now()
                WHERE acquisition_api_usage.request_count < LEAST(
                  acquisition_api_usage.request_limit,
                  EXCLUDED.request_limit
                )
                RETURNING request_count, request_limit
                """,
                (self.source_id, usage_date, self.daily_limit),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RequestBudgetExceeded(
                f"{self.source_id} daily request budget exhausted for {usage_date}"
            )
        return RequestBudgetSnapshot(
            source_id=self.source_id,
            usage_date=usage_date,
            used=int(row[0]),
            limit=min(int(row[1]), self.daily_limit),
        )

    def snapshot(self) -> RequestBudgetSnapshot:
        usage_date = self._today()
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                """
                SELECT request_count, request_limit
                FROM acquisition_api_usage
                WHERE source_id = %s AND usage_date = %s
                """,
                (self.source_id, usage_date),
            ).fetchone()
        if row is None:
            return RequestBudgetSnapshot(
                source_id=self.source_id,
                usage_date=usage_date,
                used=0,
                limit=self.daily_limit,
            )
        return RequestBudgetSnapshot(
            source_id=self.source_id,
            usage_date=usage_date,
            used=int(row[0]),
            limit=min(int(row[1]), self.daily_limit),
        )

    def exhaust(self) -> RequestBudgetSnapshot:
        """Block further calls after the upstream reports its own quota exhausted."""
        usage_date = self._today()
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                """
                INSERT INTO acquisition_api_usage(
                  source_id, usage_date, request_count, request_limit
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT(source_id, usage_date) DO UPDATE SET
                  request_count = LEAST(
                    acquisition_api_usage.request_limit,
                    EXCLUDED.request_limit
                  ),
                  request_limit = LEAST(
                    acquisition_api_usage.request_limit,
                    EXCLUDED.request_limit
                  ),
                  updated_at = now()
                RETURNING request_count, request_limit
                """,
                (self.source_id, usage_date, self.daily_limit, self.daily_limit),
            ).fetchone()
            connection.commit()
        return RequestBudgetSnapshot(
            source_id=self.source_id,
            usage_date=usage_date,
            used=int(row[0]),
            limit=int(row[1]),
        )

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
