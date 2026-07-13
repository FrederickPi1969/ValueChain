from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote

import requests


class ProxyPoolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProxyEndpoint:
    host: str
    port: int
    username: str
    password: str

    @property
    def url(self) -> str:
        username = quote(self.username, safe="")
        password = quote(self.password, safe="")
        return f"http://{username}:{password}@{self.host}:{self.port}"

    @property
    def masked(self) -> str:
        return f"{self.host}:{self.port}"


def parse_normal_proxy(value: str) -> ProxyEndpoint:
    parts = value.strip().split(":", 3)
    if len(parts) != 4:
        raise ProxyPoolError("Expected normal proxy format host:port:username:password")
    host, port_text, username, password = parts
    if not host or not username or not password:
        raise ProxyPoolError("Proxy response contains an empty required field")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ProxyPoolError("Proxy response contains an invalid port") from exc
    if not 1 <= port <= 65535:
        raise ProxyPoolError("Proxy response port is outside the valid range")
    return ProxyEndpoint(host=host, port=port, username=username, password=password)


class ProxyPoolClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        max_retries: int = 5,
        backoff_seconds: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = min(5, max(0, max_retries))
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.session = requests.Session()

    def health(self) -> dict:
        return self._get_json("/health")

    def random_normal(self) -> ProxyEndpoint:
        payload = self._get_json("/proxy/random/normal")
        value = payload.get("proxy")
        if not isinstance(value, str):
            raise ProxyPoolError("Proxy pool response is missing the proxy string")
        return parse_normal_proxy(value)

    def _get_json(self, path: str) -> dict:
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    f"{self.base_url}{path}",
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("proxy pool returned a non-object response")
                return payload
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(self.backoff_seconds * (2**attempt), 8.0))
        raise ProxyPoolError(
            f"Proxy pool request failed after retries: {type(last_error).__name__}"
        ) from last_error
