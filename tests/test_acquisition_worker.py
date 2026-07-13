import asyncio

import pytest

from valuechain import acquisition_worker
from valuechain.acquisition_worker import (
    acquisition_process_lock,
    batch_work_count,
    run_worker_loop,
)


class FakeLockConnection:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.queries: list[tuple[str, tuple[str, ...]]] = []
        self.closed = False

    def execute(self, query: str, params: tuple[str, ...]) -> "FakeLockConnection":
        self.queries.append((query, params))
        return self

    def fetchone(self) -> tuple[bool]:
        return (self.acquired,)

    def close(self) -> None:
        self.closed = True


def test_batch_work_count_ignores_discovery_metadata() -> None:
    assert batch_work_count(
        {"counts": {"discovered": 100, "issuers": 4, "documents": 12}}
    ) == 16


def test_acquisition_process_lock_is_source_scoped_and_released(monkeypatch) -> None:
    connection = FakeLockConnection(acquired=True)
    connect_args: list[tuple[str, bool]] = []

    def connect(url: str, *, autocommit: bool) -> FakeLockConnection:
        connect_args.append((url, autocommit))
        return connection

    monkeypatch.setattr(acquisition_worker.psycopg, "connect", connect)

    with acquisition_process_lock("postgresql://test", "sec_edgar"):
        assert not connection.closed

    assert connection.closed
    assert connect_args == [("postgresql://test", True)]
    assert len(connection.queries) == 2
    assert connection.queries[0][1] == ("valuechain-acquisition:sec_edgar",)
    assert "pg_try_advisory_lock" in connection.queries[0][0]
    assert "pg_advisory_unlock" in connection.queries[1][0]


def test_acquisition_process_lock_rejects_second_coordinator(monkeypatch) -> None:
    connection = FakeLockConnection(acquired=False)
    monkeypatch.setattr(
        acquisition_worker.psycopg,
        "connect",
        lambda _url, *, autocommit: connection,
    )

    with pytest.raises(RuntimeError, match="already running for cninfo"):
        with acquisition_process_lock("postgresql://test", "cninfo"):
            raise AssertionError("lock body should not run")

    assert connection.closed


def test_worker_loop_continues_after_transient_failure(capsys) -> None:
    calls = 0

    async def batch() -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary")
        return {"counts": {"issuers": 4, "documents": 8}}

    asyncio.run(
        run_worker_loop(
            batch,
            active_sleep_seconds=0,
            idle_sleep_seconds=0,
            error_sleep_seconds=0,
            max_batches=2,
        )
    )

    output = capsys.readouterr().out
    assert calls == 2
    assert "worker_batch_failed" in output
    assert '"issuers": 4' in output
    assert '"elapsed_seconds"' in output
