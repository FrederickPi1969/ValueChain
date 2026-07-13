from __future__ import annotations

import hashlib
import json
import random
import socket
import time
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlparse

import httpx

from gcu.config import Settings
from valuechain.proxy_pool import ProxyPoolClient


class NetworkBlockedError(RuntimeError):
    """Raised when the runtime cannot resolve or reach an external host."""


class HttpRequestError(RuntimeError):
    """HTTP failure with enough context to make failures actionable."""


class PayloadValidationError(RuntimeError):
    """Raised when a response is HTML/JSON masquerading as the expected binary."""


@dataclass(slots=True)
class DownloadedPayload:
    temporary_path: Path
    sha256: str
    content_length: int
    media_type: str | None
    http_status: int
    final_url: str
    response_headers: dict[str, str]
    first_bytes: bytes


class HostRateLimiter:
    """Simple sequential host limiter; intentionally no concurrency."""

    def __init__(self, default_requests_per_second: float) -> None:
        self.default_requests_per_second = default_requests_per_second
        self._last_request_at: dict[str, float] = defaultdict(float)
        self._overrides: dict[str, float] = {}

    def set_host_rate(self, host: str, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._overrides[host.lower()] = requests_per_second

    def wait(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        rate = self._overrides.get(host, self.default_requests_per_second)
        minimum_interval = 1.0 / rate
        elapsed = time.monotonic() - self._last_request_at[host]
        if elapsed < minimum_interval:
            time.sleep(minimum_interval - elapsed)
        self._last_request_at[host] = time.monotonic()


class PoliteHttpClient:
    """Declared identity, sequential throttling, retries, streaming hashes, and redacted errors."""

    RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rate_limiter = HostRateLimiter(settings.default_requests_per_second)
        self.rate_limiter.set_host_rate("www.sec.gov", settings.sec_requests_per_second)
        self.rate_limiter.set_host_rate("data.sec.gov", settings.sec_requests_per_second)
        self.proxy_pool = (
            ProxyPoolClient(settings.proxy_pool_url) if settings.proxy_pool_url else None
        )
        proxy_url = self.proxy_pool.random_normal().url if self.proxy_pool else None
        self.client = self._build_client(proxy_url)

    def _build_client(self, proxy_url: str | None) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(self.settings.http_timeout_seconds),
            follow_redirects=True,
            verify=self.settings.verify_tls,
            headers={
                "User-Agent": self.settings.user_agent,
                "From": self.settings.contact_email,
                "Accept-Encoding": "gzip, deflate",
            },
            proxy=proxy_url,
        )

    def rotate_proxy(self) -> bool:
        """Replace the HTTP transport after a source-level payload validation failure."""
        if self.proxy_pool is None:
            return False
        replacement = self._build_client(self.proxy_pool.random_normal().url)
        previous = self.client
        self.client = replacement
        previous.close()
        return True

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> PoliteHttpClient:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @staticmethod
    def _looks_like_network_block(exc: BaseException) -> bool:
        message = str(exc).lower()
        markers = (
            "name or service not known",
            "temporary failure in name resolution",
            "nodename nor servname",
            "network is unreachable",
            "no route to host",
            "dns",
        )
        return isinstance(exc, (socket.gaierror, httpx.ConnectError)) and any(
            marker in message for marker in markers
        )

    @staticmethod
    def _safe_url(url: str) -> str:
        parsed = urlparse(url)
        return parsed._replace(query="<redacted>" if parsed.query else "").geturl()

    def _backoff_seconds(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), 120.0)
        return min(2**attempt, 30) + random.uniform(0.0, 0.25)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Any = None,
        json_body: Any = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
    ) -> httpx.Response:
        last_error: BaseException | None = None
        for attempt in range(self.settings.http_max_retries + 1):
            self.rate_limiter.wait(url)
            response: httpx.Response | None = None
            try:
                response = self.client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    data=data,
                    json=json_body,
                    auth=auth,
                )
                if (
                    response.status_code in self.RETRYABLE_STATUS_CODES
                    and attempt < self.settings.http_max_retries
                ):
                    time.sleep(self._backoff_seconds(attempt, response))
                    continue
                response.raise_for_status()
                return response
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                if self._looks_like_network_block(exc):
                    raise NetworkBlockedError(
                        f"Network blocked while requesting {self._safe_url(url)}: {exc}"
                    ) from exc
                if attempt >= self.settings.http_max_retries:
                    break
                time.sleep(self._backoff_seconds(attempt, response))
        raise HttpRequestError(
            f"Request failed after retries: {method} {self._safe_url(url)}: {last_error}"
        )

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
    ) -> Any:
        response = self.request("GET", url, params=params, headers=headers, auth=auth)
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            prefix = response.text[:300].replace("\n", " ")
            raise HttpRequestError(
                f"Expected JSON from {self._safe_url(url)}, got "
                f"{response.headers.get('content-type')}: {prefix}"
            ) from exc

    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
    ) -> str:
        return self.request("GET", url, params=params, headers=headers, auth=auth).text

    @staticmethod
    def validate_payload(
        payload: DownloadedPayload,
        expected_media_type: str | None,
        filename: str,
    ) -> None:
        if payload.content_length == 0:
            raise PayloadValidationError("downloaded payload is empty")
        lowered_type = (payload.media_type or "").lower()
        lowered_expected = (expected_media_type or "").lower()
        expects_html = lowered_expected in {"text/html", "application/xhtml+xml"}
        expects_json = lowered_expected == "application/json"
        first = payload.first_bytes.lstrip().lower()
        suffix = Path(filename).suffix.lower()
        if expected_media_type and lowered_type:
            expected_family = expected_media_type.split("/", 1)[0]
            actual_family = lowered_type.split("/", 1)[0]
            compatible_html = expects_html and lowered_type in {
                "text/html",
                "application/xhtml+xml",
            }
            compatible_pdf_stream = (
                expected_media_type == "application/pdf"
                and lowered_type == "application/octet-stream"
            )
            compatible_mislabeled_json = (
                expects_json
                and lowered_type in {"text/html", "text/plain"}
                and first.startswith((b"{", b"["))
            )
            if expected_family != actual_family and not (
                compatible_html
                or compatible_pdf_stream
                or compatible_mislabeled_json
            ):
                raise PayloadValidationError(
                    f"expected {expected_media_type}, received {payload.media_type}"
                )
        if suffix == ".pdf" and not payload.first_bytes.startswith(b"%PDF-"):
            raise PayloadValidationError("PDF filename did not contain a PDF signature")
        if suffix in {
            ".zip",
            ".docx",
            ".xlsx",
            ".xbrl.zip",
            ".csv.zip",
        } and not payload.first_bytes.startswith(b"PK"):
            raise PayloadValidationError("ZIP-based filename did not contain a ZIP signature")
        if not expects_html and (
            first.startswith(b"<!doctype html") or first.startswith(b"<html")
        ):
            raise PayloadValidationError("received HTML instead of the requested document")

    @contextmanager
    def stream_to_temporary_file(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
    ) -> Iterator[DownloadedPayload]:
        last_error: BaseException | None = None
        temp_path: Path | None = None
        for attempt in range(self.settings.http_max_retries + 1):
            response: httpx.Response | None = None
            try:
                self.rate_limiter.wait(url)
                with self.client.stream(
                    "GET", url, params=params, headers=headers, auth=auth
                ) as response:
                    if (
                        response.status_code in self.RETRYABLE_STATUS_CODES
                        and attempt < self.settings.http_max_retries
                    ):
                        time.sleep(self._backoff_seconds(attempt, response))
                        continue
                    response.raise_for_status()
                    digest = hashlib.sha256()
                    size = 0
                    first_bytes = b""
                    with NamedTemporaryFile(prefix="gcu-", suffix=".part", delete=False) as temp:
                        temp_path = Path(temp.name)
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            if len(first_bytes) < 64:
                                first_bytes += chunk[: 64 - len(first_bytes)]
                            digest.update(chunk)
                            size += len(chunk)
                            temp.write(chunk)
                    payload = DownloadedPayload(
                        temporary_path=temp_path,
                        sha256=digest.hexdigest(),
                        content_length=size,
                        media_type=response.headers.get("content-type", "").split(";", 1)[0]
                        or None,
                        http_status=response.status_code,
                        final_url=str(response.url),
                        response_headers=dict(response.headers),
                        first_bytes=first_bytes,
                    )
                    yield payload
                    return
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
                    temp_path = None
                if self._looks_like_network_block(exc):
                    raise NetworkBlockedError(
                        f"Network blocked while downloading {self._safe_url(url)}: {exc}"
                    ) from exc
                if attempt >= self.settings.http_max_retries:
                    status = response.status_code if response is not None else None
                    raise HttpRequestError(
                        f"Download failed for {self._safe_url(url)}; status={status}: {exc}"
                    ) from exc
                time.sleep(self._backoff_seconds(attempt, response))
            finally:
                if temp_path is not None and temp_path.exists():
                    temp_path.unlink(missing_ok=True)
                    temp_path = None
        raise HttpRequestError(
            f"Download failed after retries for {self._safe_url(url)}: {last_error}"
        )
