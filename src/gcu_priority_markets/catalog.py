from __future__ import annotations

import csv
import io
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from gcu.models import SourceDefinition


CATALOG_PACKAGE = "gcu_priority_markets"


def _resource_text(name: str) -> str:
    return files(CATALOG_PACKAGE).joinpath("catalog", name).read_text(encoding="utf-8")


def load_overlay(path: Path | None = None) -> list[SourceDefinition]:
    text = path.read_text(encoding="utf-8") if path else _resource_text("source_overlay.yaml")
    payload = yaml.safe_load(text)
    return [SourceDefinition.model_validate(item) for item in payload["sources"]]


def load_contracts(path: Path | None = None) -> dict[str, dict[str, Any]]:
    text = path.read_text(encoding="utf-8") if path else _resource_text("source_contracts.yaml")
    payload = yaml.safe_load(text)
    return payload["contracts"]


def load_priority_markets(path: Path | None = None) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig") if path else _resource_text("priority_markets.csv")
    return list(csv.DictReader(io.StringIO(text)))
