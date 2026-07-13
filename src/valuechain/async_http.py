from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import httpx

from gcu.http import DownloadedPayload, PayloadValidationError, PoliteHttpClient
from valuechain.proxy_pool import ProxyPoolClient


RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


class AsyncHttpError(RuntimeError):
    pass


class SlowTransferError(AsyncHttpError):
    pass


class AdaptiveRateLimiter:
    """Reserve globally spaced request slots and back off after source throttling."""

    def __init__(
        self,
        requests_per_second: float,
        *,
        minimum_requests_per_second: float = 0.25,
        recovery_successes: int = 40,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.maximum_rate = requests_per_second
        self.minimum_rate = min(minimum_requests_per_second, requests_per_second)
        self.current_rate = requests_per_second
        self.recovery_successes = max(1, recovery_successes)
        self._successes = 0
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            reserved = max(now, self._next_slot)
            self._next_slot = reserved + (1.0 / self.current_rate)
        delay = reserved - now
        if delay > 0:
            await asyncio.sleep(delay)

    async def throttled(self) -> None:
        async with self._lock:
            self.current_rate = max(self.minimum_rate, self.current_rate / 2.0)
            self._successes = 0

    async def succeeded(self) -> None:
        async with self._lock:
            if self.current_rate >= self.maximum_rate:
                return
            self._successes += 1
            if self._successes >= self.recovery_successes:
                self.current_rate = min(self.maximum_rate, self.current_rate * 1.25)
                self._successes = 0


@dataclass(frozen=True)
class AsyncDownloadResult:
    source_url: str
    local_path: str
    content_type: str
    byte_size: int
    sha256: str
    retrieved_at: str
    status: str
    cached: bool
    resumed_from: int
    final_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "local_path": self.local_path,
            "content_type": self.content_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "retrieved_at": self.retrieved_at,
            "status": self.status,
            "cached": self.cached,
            "resumed_from": self.resumed_from,
            "final_url": self.final_url,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_prefix(path: Path, length: int = 64) -> bytes:
    with path.open("rb") as handle:
        return handle.read(length)


def describe_http_error(error: BaseException | None) -> str:
    if error is None:
        return "unknown error"
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTPStatusError(status={error.response.status_code})"
    return type(error).__name__


def unsatisfied_range_total(content_range: str) -> int | None:
    match = re.fullmatch(r"bytes\s+\*/(\d+)", content_range.strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


class AsyncHttpClient:
    """One-worker async transport with shared rate limiting and resumable downloads."""

    def __init__(
        self,
        *,
        limiter: AdaptiveRateLimiter,
        user_agent: str,
        contact_email: str = "",
        timeout_seconds: float = 60.0,
        max_retries: int = 5,
        proxy_pool: ProxyPoolClient | None = None,
        proxy_url: str | None = None,
        verify_tls: bool = True,
        backoff_base_seconds: float = 0.5,
        minimum_download_bytes_per_second: float = 64 * 1024,
        throughput_window_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.limiter = limiter
        self.user_agent = user_agent
        self.contact_email = contact_email
        self.timeout_seconds = timeout_seconds
        self.max_retries = min(5, max(0, max_retries))
        self.proxy_pool = proxy_pool
        self.proxy_url = proxy_url
        self.verify_tls = verify_tls
        self.backoff_base_seconds = max(0.0, backoff_base_seconds)
        self.minimum_download_bytes_per_second = max(
            0.0, minimum_download_bytes_per_second
        )
        self.throughput_window_seconds = max(0.0, throughput_window_seconds)
        self.transport = transport
        self.client = self._build_client()

    @classmethod
    async def create(
        cls,
        *,
        proxy_pool: ProxyPoolClient,
        limiter: AdaptiveRateLimiter,
        user_agent: str,
        contact_email: str = "",
        timeout_seconds: float = 60.0,
        max_retries: int = 5,
        verify_tls: bool = True,
    ) -> AsyncHttpClient:
        endpoint = await asyncio.to_thread(proxy_pool.random_normal)
        return cls(
            limiter=limiter,
            user_agent=user_agent,
            contact_email=contact_email,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            proxy_pool=proxy_pool,
            proxy_url=endpoint.url,
            verify_tls=verify_tls,
        )

    def _build_client(self) -> httpx.AsyncClient:
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        if self.contact_email:
            headers["From"] = self.contact_email
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=True,
            verify=self.verify_tls,
            headers=headers,
            proxy=self.proxy_url,
            transport=self.transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> AsyncHttpClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def rotate_proxy(self) -> bool:
        if self.proxy_pool is None:
            return False
        endpoint = await asyncio.to_thread(self.proxy_pool.random_normal)
        previous = self.client
        self.proxy_url = endpoint.url
        self.client = self._build_client()
        await previous.aclose()
        return True

    async def _retry_wait(self, attempt: int, response: httpx.Response | None) -> None:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                await asyncio.sleep(min(float(retry_after), 120.0))
                return
        delay = min(self.backoff_base_seconds * (2**attempt), 8.0)
        if delay:
            await asyncio.sleep(delay + random.uniform(0.0, min(0.25, delay / 4)))

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Any = None,
        json_body: Any = None,
    ) -> httpx.Response:
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            response: httpx.Response | None = None
            await self.limiter.acquire()
            try:
                response = await self.client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    data=data,
                    json=json_body,
                )
                if response.status_code in RETRYABLE_STATUS_CODES:
                    await self.limiter.throttled()
                    if attempt < self.max_retries:
                        await self.rotate_proxy()
                        await self._retry_wait(attempt, response)
                        continue
                response.raise_for_status()
                await self.limiter.succeeded()
                return response
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                await self.rotate_proxy()
                await self._retry_wait(attempt, response)
        raise AsyncHttpError(
            f"Request failed after retries: {method} {PoliteHttpClient._safe_url(url)}: "
            f"{describe_http_error(last_error)}"
        ) from last_error

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self.request("GET", url, params=params, headers=headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AsyncHttpError(f"Expected JSON from {PoliteHttpClient._safe_url(url)}") from exc
        if not isinstance(payload, dict):
            raise AsyncHttpError(f"Expected JSON object from {PoliteHttpClient._safe_url(url)}")
        return payload

    async def post_json(
        self,
        url: str,
        *,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self.request("POST", url, data=data, headers=headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AsyncHttpError(f"Expected JSON from {PoliteHttpClient._safe_url(url)}") from exc
        if not isinstance(payload, dict):
            raise AsyncHttpError(f"Expected JSON object from {PoliteHttpClient._safe_url(url)}")
        return payload

    async def download(
        self,
        url: str,
        output_path: Path,
        *,
        expected_media_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists() and output_path.stat().st_size > 0:
            stat = output_path.stat()
            return AsyncDownloadResult(
                source_url=url,
                local_path=str(output_path),
                content_type="",
                byte_size=stat.st_size,
                sha256=await asyncio.to_thread(sha256_file, output_path),
                retrieved_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                status="complete",
                cached=True,
                resumed_from=0,
                final_url=url,
            ).as_dict()

        partial = output_path.with_name(f"{output_path.name}.partial")
        last_error: BaseException | None = None
        successful_resume_from = 0
        for attempt in range(self.max_retries + 1):
            response: httpx.Response | None = None
            resume_from = partial.stat().st_size if partial.exists() else 0
            request_headers = dict(headers or {})
            request_headers.setdefault("Accept-Encoding", "identity")
            if resume_from:
                request_headers["Range"] = f"bytes={resume_from}-"
            await self.limiter.acquire()
            try:
                async with self.client.stream(
                    "GET", url, headers=request_headers or None
                ) as response:
                    if response.status_code == 416 and resume_from:
                        remote_size = unsatisfied_range_total(
                            response.headers.get("Content-Range", "")
                        )
                        if remote_size == resume_from:
                            first_bytes = await asyncio.to_thread(read_prefix, partial)
                            media_type = expected_media_type or ""
                            payload = DownloadedPayload(
                                temporary_path=partial,
                                sha256="",
                                content_length=resume_from,
                                media_type=media_type or None,
                                http_status=response.status_code,
                                final_url=str(response.url),
                                response_headers=dict(response.headers),
                                first_bytes=first_bytes,
                            )
                            PoliteHttpClient.validate_payload(
                                payload, expected_media_type, output_path.name
                            )
                            final_url = str(response.url)
                            successful_resume_from = resume_from
                            await self.limiter.succeeded()
                        else:
                            partial.unlink(missing_ok=True)
                            response.raise_for_status()
                    else:
                        if response.status_code in RETRYABLE_STATUS_CODES:
                            await self.limiter.throttled()
                            response.raise_for_status()
                        response.raise_for_status()
                        append = resume_from > 0 and response.status_code == 206
                        if resume_from and not append:
                            resume_from = 0
                        successful_resume_from = resume_from if append else 0
                        mode = "ab" if append else "wb"
                        with partial.open(mode) as handle:
                            loop = asyncio.get_running_loop()
                            window_started = loop.time()
                            window_bytes = 0
                            async for chunk in response.aiter_bytes(
                                chunk_size=1024 * 1024
                            ):
                                if chunk:
                                    handle.write(chunk)
                                    window_bytes += len(chunk)
                                elapsed = loop.time() - window_started
                                if elapsed >= self.throughput_window_seconds:
                                    throughput = window_bytes / max(elapsed, 0.001)
                                    if (
                                        self.minimum_download_bytes_per_second
                                        and throughput
                                        < self.minimum_download_bytes_per_second
                                    ):
                                        raise SlowTransferError(
                                            f"download throughput {throughput:.0f} B/s is below "
                                            f"{self.minimum_download_bytes_per_second:.0f} B/s"
                                        )
                                    window_started = loop.time()
                                    window_bytes = 0
                            handle.flush()
                            await asyncio.to_thread(os.fsync, handle.fileno())

                        first_bytes = await asyncio.to_thread(read_prefix, partial)
                        media_type = response.headers.get("content-type", "").split(
                            ";", 1
                        )[0]
                        payload = DownloadedPayload(
                            temporary_path=partial,
                            sha256="",
                            content_length=partial.stat().st_size,
                            media_type=media_type or None,
                            http_status=response.status_code,
                            final_url=str(response.url),
                            response_headers=dict(response.headers),
                            first_bytes=first_bytes,
                        )
                        PoliteHttpClient.validate_payload(
                            payload, expected_media_type, output_path.name
                        )
                        final_url = str(response.url)
                        await self.limiter.succeeded()
                os.replace(partial, output_path)
                return AsyncDownloadResult(
                    source_url=url,
                    local_path=str(output_path),
                    content_type=media_type,
                    byte_size=output_path.stat().st_size,
                    sha256=await asyncio.to_thread(sha256_file, output_path),
                    retrieved_at=datetime.now(UTC).isoformat(),
                    status="complete",
                    cached=False,
                    resumed_from=successful_resume_from,
                    final_url=final_url,
                ).as_dict()
            except PayloadValidationError as exc:
                last_error = exc
                partial.unlink(missing_ok=True)
            except (httpx.HTTPError, OSError, SlowTransferError) as exc:
                last_error = exc
            if attempt >= self.max_retries:
                break
            await self.rotate_proxy()
            await self._retry_wait(attempt, response)
        raise AsyncHttpError(
            f"Download failed after retries: {PoliteHttpClient._safe_url(url)}: "
            f"{describe_http_error(last_error)}"
        ) from last_error
