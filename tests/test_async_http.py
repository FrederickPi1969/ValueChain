import asyncio
from pathlib import Path

import httpx
import pytest

from valuechain.async_http import AdaptiveRateLimiter, AsyncHttpClient, AsyncHttpError


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


def test_async_request_reserves_budget_for_each_attempt() -> None:
    async def run() -> tuple[int, int]:
        attempts = 0
        reservations = 0

        async def reserve() -> None:
            nonlocal reservations
            reservations += 1

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503 if attempts == 1 else 200, json={"ok": True})

        client = AsyncHttpClient(
            limiter=AdaptiveRateLimiter(10_000),
            user_agent="test",
            max_retries=1,
            backoff_base_seconds=0,
            transport=httpx.MockTransport(handler),
            before_request=reserve,
        )
        try:
            await client.get_json("https://example.test/data")
        finally:
            await client.close()
        return attempts, reservations

    assert asyncio.run(run()) == (2, 2)


def test_async_request_error_includes_http_status() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async def run() -> None:
        client = AsyncHttpClient(
            limiter=AdaptiveRateLimiter(10_000),
            user_agent="test",
            max_retries=0,
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(AsyncHttpError, match=r"HTTPStatusError\(status=404\)"):
                await client.request("GET", "https://example.test/missing")
        finally:
            await client.close()

    asyncio.run(run())


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


def test_async_download_keeps_credentials_out_of_result_url(tmp_path: Path) -> None:
    target = tmp_path / "filing.zip"
    reservations = 0

    async def reserve() -> None:
        nonlocal reservations
        reservations += 1

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["crtfc_key"] == "secret"
        assert request.url.params["rcept_no"] == "20260714000001"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/zip"},
            content=b"PK\x03\x04payload",
        )

    async def run() -> dict:
        client = AsyncHttpClient(
            limiter=AdaptiveRateLimiter(10_000),
            user_agent="test",
            max_retries=0,
            transport=httpx.MockTransport(handler),
            before_request=reserve,
        )
        try:
            return await client.download(
                "https://example.test/document.xml?rcept_no=20260714000001",
                target,
                expected_media_type="application/zip",
                params={"crtfc_key": "secret"},
            )
        finally:
            await client.close()

    result = asyncio.run(run())

    assert reservations == 1
    assert "secret" not in result["source_url"]
    assert "secret" not in result["final_url"]
    assert result["final_url"].endswith("?<redacted>")


def test_async_download_finalizes_complete_partial_after_416(tmp_path: Path) -> None:
    content = b"%PDF-" + (b"complete" * 100)
    target = tmp_path / "report.pdf"
    partial = tmp_path / "report.pdf.partial"
    partial.write_bytes(content)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Range"] == f"bytes={len(content)}-"
        return httpx.Response(
            416,
            headers={"Content-Range": f"bytes */{len(content)}"},
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

    assert target.read_bytes() == content
    assert result["resumed_from"] == len(content)
    assert not partial.exists()


def test_async_download_restarts_when_416_size_does_not_match(tmp_path: Path) -> None:
    content = b"%PDF-" + (b"replacement" * 100)
    target = tmp_path / "report.pdf"
    partial = tmp_path / "report.pdf.partial"
    partial.write_bytes(b"%PDF-incomplete")
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "Range" in request.headers
            return httpx.Response(416, headers={"Content-Range": "bytes */999"})
        assert "Range" not in request.headers
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=content,
        )

    async def run() -> dict:
        client = AsyncHttpClient(
            limiter=AdaptiveRateLimiter(10_000),
            user_agent="test",
            max_retries=1,
            backoff_base_seconds=0,
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

    assert calls == 2
    assert target.read_bytes() == content
    assert result["resumed_from"] == 0
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


def test_slow_download_keeps_partial_file_for_next_worker(tmp_path: Path) -> None:
    target = tmp_path / "large.bin"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (1024 * 1024))

    async def run() -> None:
        client = AsyncHttpClient(
            limiter=AdaptiveRateLimiter(10_000),
            user_agent="test",
            max_retries=0,
            minimum_download_bytes_per_second=10**20,
            throughput_window_seconds=0,
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(AsyncHttpError, match="SlowTransferError"):
                await client.download("https://example.test/large.bin", target)
        finally:
            await client.close()

    asyncio.run(run())

    assert not target.exists()
    assert (tmp_path / "large.bin.partial").stat().st_size > 0
