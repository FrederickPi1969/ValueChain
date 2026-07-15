from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class BenchmarkCase:
    case_id: str
    task: str
    source: str
    text: str
    question: str = ""
    gold: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DocumentChunk:
    chunk_id: str
    text: str
    page: int | None = None
    section_hint: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExtractionRecord:
    company_id: str
    company_name: str
    ticker: str
    filing_id: str
    filing_form: str
    filing_date: str
    classification: dict[str, Any]
    financial_facts: list[dict[str, Any]]
    material_signals: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    extractor_model: str
    extractor_version: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
