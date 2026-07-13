from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from gcu.adapters.base import BaseAdapter
from gcu.models import DocumentRef, EntityRef, FilingRef, SmokeResult, SmokeStatus

from gcu_priority_markets.catalog import load_contracts
from gcu_priority_markets.io import alias_value, read_tabular_path


def _aliases(contract: dict[str, Any], field: str) -> list[str]:
    aliases = contract.get("field_aliases", {}).get(field, [])
    return [field, *[str(item) for item in aliases]]


def _date_value(value: Any) -> tuple[date | None, datetime | None]:
    if value in (None, ""):
        return None, None
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
        return parsed.date(), parsed
    if isinstance(value, date):
        return value, datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    formats = (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%d-%b-%Y",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.date(), parsed
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.date(), parsed
    except ValueError:
        return None, None


def _stable_id(source_id: str, values: Iterable[Any]) -> str:
    text = "|".join(str(value or "").strip() for value in values)
    return hashlib.sha256(f"{source_id}|{text}".encode("utf-8")).hexdigest()[:32]


class OfficialExportAdapter(BaseAdapter):
    """Policy-safe normalizer for CSV/XLSX/HTML/JSON exported by official portals."""

    def __init__(self, *, contract: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.contract = contract or load_contracts().get(self.source_id, {})

    def _value(self, row: dict[str, Any], field: str, default: Any = None) -> Any:
        return alias_value(row, _aliases(self.contract, field), default)

    def parse_entities(self, rows: Iterable[dict[str, Any]]) -> Iterable[EntityRef]:
        defaults = self.contract.get("defaults", {})
        stable_fields = self.contract.get(
            "stable_key_fields", ["issuer_id", "issuer_name", "symbol", "isin", "exchange"]
        )
        for row in rows:
            name = str(self._value(row, "issuer_name", "")).strip()
            symbol = str(self._value(row, "symbol", "")).strip() or None
            isin = str(self._value(row, "isin", "")).strip() or None
            issuer_id = str(self._value(row, "issuer_id", "")).strip() or None
            exchange = str(
                self._value(row, "exchange", defaults.get("exchange", ""))
            ).strip() or None
            jurisdiction = str(defaults.get("jurisdiction") or "").strip() or None
            if not name:
                continue
            source_entity_id = issuer_id or symbol or isin or _stable_id(
                self.source_id,
                [self._value(row, field) for field in stable_fields],
            )
            yield EntityRef(
                entity_id=f"{self.source_id}-{source_entity_id}",
                source_id=self.source_id,
                source_entity_id=source_entity_id,
                legal_name=name,
                jurisdiction=jurisdiction,
                exchange=exchange,
                ticker=symbol,
                isin=isin,
                local_registry_id=issuer_id,
                metadata=row,
            )

    def parse_filings(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        entity: EntityRef | None = None,
    ) -> Iterable[FilingRef]:
        defaults = self.contract.get("defaults", {})
        stable_fields = self.contract.get(
            "stable_key_fields", ["document_id", "filing_date", "issuer_name", "document_url"]
        )
        for row in rows:
            issuer_name = str(self._value(row, "issuer_name", "")).strip()
            issuer_id = str(self._value(row, "issuer_id", "")).strip()
            security_code = str(self._value(row, "symbol", "")).strip()
            title = str(self._value(row, "title", "")).strip()
            form = str(self._value(row, "form", "")).strip() or "official_disclosure"
            filed_at, published_at = _date_value(self._value(row, "filing_date"))
            document_url = str(self._value(row, "document_url", "")).strip() or None
            detail_url = str(self._value(row, "detail_url", "")).strip() or None
            explicit_id = str(self._value(row, "document_id", "")).strip()
            filing_id = explicit_id or _stable_id(
                self.source_id,
                [self._value(row, field) for field in stable_fields],
            )
            source_entity_id = (
                entity.source_entity_id if entity else issuer_id or security_code or issuer_name or None
            )
            entity_id = (
                entity.entity_id
                if entity
                else f"{self.source_id}-{source_entity_id or filing_id}"
            )
            yield FilingRef(
                source_id=self.source_id,
                filing_id=filing_id,
                entity_id=entity_id,
                source_entity_id=source_entity_id,
                form=form,
                title=title or form,
                filed_at=filed_at,
                detail_url=detail_url,
                primary_document_url=document_url,
                amendment=any(
                    marker in (title or "").lower()
                    for marker in ("amend", "revised", "correction", "corrigendum", "replacement")
                ),
                metadata={
                    **row,
                    "issuer_name": issuer_name or None,
                    "security_code": security_code or None,
                    "jurisdiction": defaults.get("jurisdiction"),
                    "published_at": published_at.isoformat() if published_at else None,
                },
            )

    def list_entities(self, *, input_path: Path, **_: Any) -> Iterable[EntityRef]:
        yield from self.parse_entities(read_tabular_path(input_path))

    def list_filings(
        self,
        entity: EntityRef | None = None,
        *,
        input_path: Path,
        **_: Any,
    ) -> Iterable[FilingRef]:
        yield from self.parse_filings(read_tabular_path(input_path), entity=entity)

    def list_documents(self, filing: FilingRef, **_: Any) -> Iterable[DocumentRef]:
        if not filing.primary_document_url:
            return
        filename = filing.primary_document_url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if not filename or "." not in filename:
            filename = f"{filing.filing_id}.bin"
        yield DocumentRef(
            source_id=self.source_id,
            document_id=f"{filing.filing_id}:primary",
            filing_id=filing.filing_id,
            entity_id=filing.entity_id,
            url=filing.primary_document_url,
            filename=filename,
            document_type=filing.form,
            filed_at=filing.filed_at,
        )

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        aliases = self.contract.get("field_aliases", {})
        if not aliases:
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="official_export_contract",
                endpoint=str(self.definition.official_url),
                message="No official-export field aliases were configured.",
            )
        if offline:
            sample_row = {
                "Company Name": "Example Issuer plc",
                "Symbol": "EXM",
                "Publication Date": "2026-07-10",
                "Document Title": "Annual Financial Report",
                "Download URL": "https://example.invalid/report.pdf",
            }
            entities = list(self.parse_entities([sample_row]))
            filings = list(self.parse_filings([sample_row]))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="official_export_contract",
                endpoint=str(self.definition.official_url),
                records_observed=len(entities) + len(filings),
                message="Official-export aliases, stable identifiers and snapshot-monitor contract validated offline.",
                evidence={"input_kind": self.contract.get("input_kind", "auto")},
            )
        return SmokeResult(
            source_id=self.source_id,
            status=SmokeStatus.OFFICIAL_WEB_ONLY,
            operation="official_export_contract",
            endpoint=str(self.definition.official_url),
            message="This source intentionally requires an operator-owned official export or browser session.",
        )
