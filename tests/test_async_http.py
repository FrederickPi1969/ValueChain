import asyncio
from pathlib import Path

import httpx

from valuechain.async_http import AdaptiveRateLimiter, AsyncHttpClient


def test_async_request_retries_and_recovers() -> None:
    async def run() -> tuple[dict[str, bool], int]:
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, json={"error": "temporary"})
            return httpx.Response(200, json={"ok": True})

        client = AsyncHttpClient(
            limiter=AdaptiveRateLimiter(10_000),
            user_agent="test",
            max_retries=5,
            backoff_base_seconds=0,
            transport=httpx.MockTransport(handler),
        )
        try:
            payload = await client.get_json("https://example.test/data")
        finally:
            await client.close()
        return payload, calls

    payload, calls = asyncio.run(run())

    assert payload == {"ok": True}
    assert calls == 2


def test_async_download_resumes_partial_file(tmp_path: Path) -> None:
    content = b"%PDF-" + (b"payload" * 100)
    target = tmp_path / "report.pdf"
    partial = tmp_path / "report.pdf.partial"
    partial.write_bytes(content[:25])
    observed_range = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_range
        observed_range = request.headers.get("Range", "")
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            206,
            headers={
                "Content-Type": "application/pdf",
                "Content-Range": f"bytes 25-{len(content) - 1}/{len(content)}",
            },
            content=content[25:],
        )

    async def run() -> dict:
        client = AsyncHttpClient(
            limiter=AdaptiveRateLimiter(10_000),
            user_agent="test",
            max_retries=0,
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.download(
                "https://example.test/report.pdf",
                target,
                expected_media_type="application/pdf",
            )
        finally:
            await client.close()

    result = asyncio.run(run())

    assert observed_range == "bytes=25-"
    assert target.read_bytes() == content
    assert result["resumed_from"] == 25
    assert not partial.exists()


def test_adaptive_limiter_reduces_and_recovers_rate() -> None:
    async def run() -> tuple[float, float]:
        limiter = AdaptiveRateLimiter(8, recovery_successes=2)

        await limiter.throttled()
        throttled_rate = limiter.current_rate

        await limiter.succeeded()
        await limiter.succeeded()
        return throttled_rate, limiter.current_rate

    throttled_rate, recovered_rate = asyncio.run(run())

    assert throttled_rate == 4
    assert recovered_rate == 5
