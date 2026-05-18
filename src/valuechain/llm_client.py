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


class OpenAICompatibleClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

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
        if self.config.base_url or not self.config.report_url:
            return self.config
        return resolve_from_report(self.config)


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
