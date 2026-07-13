from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


ENTITY_FIELDS = [
    "entity_id",
    "source_id",
    "source_entity_id",
    "legal_name",
    "jurisdiction",
    "exchange",
    "ticker",
    "lei",
    "isin",
    "local_registry_id",
    "aliases",
    "metadata",
]

FILING_FIELDS = [
    "source_id",
    "filing_id",
    "entity_id",
    "source_entity_id",
    "form",
    "title",
    "filed_at",
    "period_end",
    "detail_url",
    "primary_document_url",
    "language",
    "amendment",
    "metadata",
]


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, records: Iterable[Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            if isinstance(record, BaseModel):
                handle.write(record.model_dump_json(exclude_none=True))
            else:
                handle.write(json.dumps(record, ensure_ascii=False, default=_json_default))
            handle.write("\n")
            count += 1
    return count


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def write_models_csv(path: Path, records: Iterable[BaseModel], fields: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = record.model_dump(mode="python")
            writer.writerow({key: _csv_value(row.get(key)) for key in fields})
            count += 1
    return count
