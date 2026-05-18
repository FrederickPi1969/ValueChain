from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    root_dir: Path = ROOT
    data_dir: Path = ROOT / "data"
    raw_dir: Path = ROOT / "data" / "raw"
    processed_dir: Path = ROOT / "data" / "processed"
    reports_dir: Path = ROOT / "reports"
    source_registry_path: Path = ROOT / "config" / "source_registry.yaml"
    ontology_path: Path = ROOT / "config" / "ontology.yaml"
    sec_user_agent: str = os.getenv(
        "VALUECHAIN_SEC_USER_AGENT",
        "FrederickPi ValueChainPrototype/0.1 contact=unknown@example.com",
    )
    sec_rps: float = float(os.getenv("VALUECHAIN_SEC_RPS", "2.0"))
    http_proxy: str = os.getenv("VALUECHAIN_HTTP_PROXY", "")
    https_proxy: str = os.getenv("VALUECHAIN_HTTPS_PROXY", "")
    llm_base_url: str = os.getenv("VALUECHAIN_LLM_BASE_URL", "")
    llm_api_key: str = os.getenv("VALUECHAIN_LLM_API_KEY", "1969")
    extraction_model: str = os.getenv("VALUECHAIN_EXTRACTION_MODEL", "Qwen/Qwen3.5-4B")
    complex_model: str = os.getenv("VALUECHAIN_COMPLEX_MODEL", "Qwen/Qwen3.6-35B-A3B")
    llm_report_url: str = os.getenv(
        "VALUECHAIN_LLM_REPORT_URL", "http://localllm.frederickpi.com/report"
    )
    database_url: str = os.getenv(
        "VALUECHAIN_DATABASE_URL",
        "postgresql://valuechain:valuechain_dev@127.0.0.1:5433/valuechain",
    )

    @property
    def proxies(self) -> dict[str, str]:
        proxies: dict[str, str] = {}
        if self.http_proxy:
            proxies["http://"] = self.http_proxy
        if self.https_proxy:
            proxies["https://"] = self.https_proxy
        return proxies


def ensure_dirs(settings: Settings) -> None:
    for path in [
        settings.data_dir,
        settings.raw_dir,
        settings.processed_dir,
        settings.reports_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_ontology(settings: Settings) -> dict:
    return load_yaml(settings.ontology_path)
