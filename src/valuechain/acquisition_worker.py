from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any


BatchCallable = Callable[[], Awaitable[dict[str, Any]]]


def batch_work_count(result: dict[str, Any]) -> int:
    counts = result.get("counts", {})
    if not isinstance(counts, dict):
        return int(result.get("objects", 0) or 0)
    return sum(
        int(counts.get(key, 0) or 0)
        for key in ("issuers", "filings", "documents")
    )


async def run_worker_loop(
    run_batch: BatchCallable,
    *,
    active_sleep_seconds: float = 1.0,
    idle_sleep_seconds: float = 30.0,
    error_sleep_seconds: float = 30.0,
    max_batches: int | None = None,
) -> None:
    batches = 0
    while max_batches is None or batches < max_batches:
        try:
            result = await run_batch()
            print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True), flush=True)
            delay = (
                active_sleep_seconds
                if batch_work_count(result)
                else idle_sleep_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "event": "worker_batch_failed",
                        "failed_at": datetime.now(UTC).isoformat(),
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            delay = error_sleep_seconds
        batches += 1
        if max_batches is None or batches < max_batches:
            await asyncio.sleep(max(0.0, delay))
