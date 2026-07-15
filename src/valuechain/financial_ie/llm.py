from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class AsyncLLMConfig:
    base_url: str = "http://100.114.26.88:31969/v1"
    api_key: str = "1969"
    model: str = "Qwen/Qwen3.6-35B-A3B"
    concurrency: int = 4
    timeout_s: float = 180.0
    max_retries: int = 3


@dataclass(slots=True)
class LLMResponse:
    content: str
    latency_s: float
    usage: dict[str, Any]
    attempts: int


class AsyncLLMClient:
    def __init__(self, config: AsyncLLMConfig) -> None:
        self.config = config
        self._semaphore = asyncio.Semaphore(max(1, config.concurrency))
        self._client = httpx.AsyncClient(
            timeout=config.timeout_s,
            limits=httpx.Limits(
                max_connections=max(2, config.concurrency),
                max_keepalive_connections=max(2, config.concurrency),
            ),
        )

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1000,
    ) -> LLMResponse:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with self._semaphore:
            for attempt in range(1, self.config.max_retries + 1):
                started = time.monotonic()
                try:
                    response = await self._client.post(
                        f"{self.config.base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {self.config.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                    body = response.json()
                    return LLMResponse(
                        content=str(body["choices"][0]["message"]["content"]),
                        latency_s=round(time.monotonic() - started, 4),
                        usage=dict(body.get("usage") or {}),
                        attempts=attempt,
                    )
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError):
                    if attempt >= self.config.max_retries:
                        raise
                    await asyncio.sleep((2 ** (attempt - 1)) + random.random() * 0.25)
        raise RuntimeError("unreachable")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncLLMClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
