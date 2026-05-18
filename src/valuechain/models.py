from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Company:
    ticker: str
    company_name: str
    role: str = ""
    priority: int = 3
    notes: str = ""
    cik: str = ""
    exchange: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FilingRecord:
    ticker: str
    cik: str
    company_name: str
    form: str
    accession_number: str
    filing_date: str
    report_date: str = ""
    accepted_timestamp: str = ""
    primary_document: str = ""
    archive_url: str = ""
    primary_document_url: str = ""
    local_path: str = ""
    sha256: str = ""

    def accession_no_dashes(self) -> str:
        return self.accession_number.replace("-", "")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SourceDocument:
    ticker: str
    cik: str
    company_name: str
    form: str
    accession_number: str
    filing_date: str
    report_date: str = ""
    accepted_timestamp: str = ""
    archive_url: str = ""
    document: str = ""
    document_type: str = ""
    description: str = ""
    sequence: str = ""
    document_url: str = ""
    local_path: str = ""
    sha256: str = ""
    is_primary: bool = False

    def accession_no_dashes(self) -> str:
        return self.accession_number.replace("-", "")

    def document_id(self) -> str:
        return f"{self.accession_no_dashes()}:{self.document or 'document'}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["document_id"] = self.document_id()
        return data


@dataclass(slots=True)
class Section:
    filing: FilingRecord | SourceDocument
    section_name: str
    text: str
    parser_name: str
    parser_version: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["filing"] = self.filing.to_dict()
        return data


@dataclass(slots=True)
class Passage:
    passage_id: str
    ticker: str
    cik: str
    company_name: str
    form: str
    accession_number: str
    filing_date: str
    accepted_timestamp: str
    source_document_url: str
    section: str
    paragraph_offset: int
    text: str
    parser_name: str
    parser_version: str
    source_document: str = ""
    source_document_type: str = ""
    relevance_score: float = 0.0
    relevance_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EntityMention:
    text: str
    entity_type: str
    normalized_name: str = ""
    ticker: str = ""
    cik: str = ""
    confidence: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RelationEvidence:
    subject: str
    object: str
    relation_type: str
    direction: str
    modality: str
    certainty: str
    temporal_scope: str
    evidence_text: str
    confidence_score: float
    extractor_model_version: str
    ticker: str
    cik: str
    form: str
    filing_date: str
    accepted_timestamp: str
    accession_number: str
    source_document_url: str
    source_section: str
    passage_id: str
    paragraph_offset: int
    parser_name: str
    parser_version: str
    source_document: str = ""
    source_document_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GraphEdge:
    subject: str
    object: str
    relation_type: str
    modality: str
    first_seen: str
    last_seen: str
    evidence_count: int
    avg_confidence: float
    forms: str
    accessions: str
    source_urls: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
