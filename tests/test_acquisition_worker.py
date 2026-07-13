import asyncio

from valuechain.acquisition_worker import batch_work_count, run_worker_loop


def test_batch_work_count_ignores_discovery_metadata() -> None:
    assert batch_work_count(
        {"counts": {"discovered": 100, "issuers": 4, "documents": 12}}
    ) == 16


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
