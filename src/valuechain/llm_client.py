from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

import httpx


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    report_url: str = ""
    proxy_url: str = ""
    timeout_s: int = 120
    max_connections: int = 16
    max_keepalive_connections: int = 8


class OpenAICompatibleClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._resolved_config_cache: LLMConfig | None = None
        self._async_client: httpx.AsyncClient | None = None

    def available(self) -> bool:
        return bool(self._resolved_config().base_url and self.config.model)

    def chat_json(self, system: str, user: str, max_tokens: int = 1200) -> Any:
        config = self._resolved_config()
        if not config.base_url:
            raise RuntimeError("LLM base_url/model is not configured")
        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request_kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {config.api_key}"},
            "json": payload,
            "timeout": config.timeout_s,
        }
        if config.proxy_url:
            request_kwargs["proxy"] = config.proxy_url
        response = httpx.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            **request_kwargs,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return parse_json_content(content)

    def _resolved_config(self) -> LLMConfig:
        if self._resolved_config_cache is not None:
            return self._resolved_config_cache
        if self.config.base_url or not self.config.report_url:
            self._resolved_config_cache = self.config
        else:
            self._resolved_config_cache = resolve_from_report(self.config)
        return self._resolved_config_cache

    async def chat_json_async(self, system: str, user: str, max_tokens: int = 1200) -> Any:
        config = await self._resolved_config_async()
        if not config.base_url:
            raise RuntimeError("LLM base_url/model is not configured")
        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        client = self._get_async_client(config)
        response = await client.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {config.api_key}"},
            json=payload,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return parse_json_content(content)

    async def _resolved_config_async(self) -> LLMConfig:
        if self._resolved_config_cache is not None:
            return self._resolved_config_cache
        if self.config.base_url or not self.config.report_url:
            self._resolved_config_cache = self.config
        else:
            self._resolved_config_cache = await resolve_from_report_async(self.config)
        return self._resolved_config_cache

    def _get_async_client(self, config: LLMConfig) -> httpx.AsyncClient:
        if self._async_client is not None:
            return self._async_client
        kwargs: dict[str, Any] = {
            "timeout": config.timeout_s,
            "limits": httpx.Limits(
                max_connections=config.max_connections,
                max_keepalive_connections=config.max_keepalive_connections,
            ),
        }
        if config.proxy_url:
            kwargs["proxy"] = config.proxy_url
        self._async_client = httpx.AsyncClient(**kwargs)
        return self._async_client

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None


def resolve_from_report(config: LLMConfig) -> LLMConfig:
    response = httpx.get(config.report_url, timeout=10)
    response.raise_for_status()
    report = response.json()
    if not isinstance(report, dict):
        return config
    hosts = report.get(config.model)
    if not isinstance(hosts, list) or not hosts:
        return config
    host = next((str(item) for item in hosts if isinstance(item, str) and ":virtual" not in item), "")
    if not host:
        return config
    return replace(config, base_url=f"http://{host}/v1")


async def resolve_from_report_async(config: LLMConfig) -> LLMConfig:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(config.report_url)
    response.raise_for_status()
    report = response.json()
    if not isinstance(report, dict):
        return config
    hosts = report.get(config.model)
    if not isinstance(hosts, list) or not hosts:
        return config
    host = next((str(item) for item in hosts if isinstance(item, str) and ":virtual" not in item), "")
    if not host:
        return config
    return replace(config, base_url=f"http://{host}/v1")


def parse_json_content(content: str) -> Any:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.removeprefix("json").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = min(
            [idx for idx in [content.find("["), content.find("{")] if idx >= 0],
            default=-1,
        )
        end = max(content.rfind("]"), content.rfind("}"))
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise
