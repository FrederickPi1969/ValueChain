from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from valuechain.disclosure_schema import (
    CanonicalDocumentType,
    FallbackMode,
    get_source_schema,
    normalize_source_document_type,
)


class CompanyIdentifier(StrEnum):
    AUTO = "auto"
    SOURCE_ISSUER_ID = "source_issuer_id"
    TICKER = "ticker"
    LEGAL_NAME = "legal_name"


class YearBasis(StrEnum):
    AUTO = "auto"
    REPORT_PERIOD = "report_period"
    FILING_DATE = "filing_date"


class ResolveDisclosureRequest(BaseModel):
    """Stable cross-market request contract for a disclosure or report."""

    model_config = ConfigDict(str_strip_whitespace=True, use_enum_values=True)

    company: str = Field(
        min_length=1,
        description="Company identifier value, such as CIK, ticker, corp_code, EDINET code, LEI, or exact legal name.",
        examples=["NVDA", "0000320193", "005930"],
    )
    source_id: str | None = Field(
        default=None,
        description="Canonical source id. Omit only when the company identifier is globally unambiguous.",
        examples=["sec_edgar", "cninfo", "opendart"],
    )
    company_identifier: CompanyIdentifier = Field(
        default=CompanyIdentifier.AUTO,
        description="How `company` should be matched in the source issuer registry.",
    )
    year: int = Field(
        ge=1994,
        le=2100,
        description="Requested report/fiscal year by default; event reports use filing year.",
        examples=[2025],
    )
    document_type: CanonicalDocumentType = Field(
        description="Cross-market canonical document type from `/api/acquisition/schema`.",
        examples=["annual_report"],
    )
    source_document_type: str | None = Field(
        default=None,
        description="Optional exact native form/name/code, such as `20-F`, `120`, or `사업보고서`.",
    )
    year_basis: YearBasis = Field(
        default=YearBasis.AUTO,
        description="Use report period, filing date, or the taxonomy default for year matching.",
    )
    include_amendments: bool = Field(
        default=False,
        description="Include amended/corrected reports in addition to originals.",
    )
    allow_fallback: bool = Field(
        default=True,
        description="Queue an upstream API retrieval when no complete local document exists.",
    )
    wait_seconds: int = Field(
        default=0,
        ge=0,
        le=120,
        description="Seconds to wait for the background worker. Zero returns HTTP 202 immediately on a miss.",
    )

    @model_validator(mode="after")
    def validate_source_document_type(self) -> "ResolveDisclosureRequest":
        if self.source_id:
            schema = get_source_schema(self.source_id)
            canonical_type = CanonicalDocumentType(self.document_type)
            if canonical_type not in {
                mapping.canonical_type for mapping in schema.mappings
            }:
                raise ValueError(
                    f"{self.source_id} does not map canonical type {canonical_type.value}"
                )
            if self.source_document_type:
                mapped = schema.canonicalize(self.source_document_type)
                if mapped != canonical_type:
                    raise ValueError(
                        f"source_document_type {self.source_document_type!r} maps to "
                        f"{mapped.value}, not {canonical_type.value}"
                    )
        return self


def request_key(
    request: ResolveDisclosureRequest, source_id: str, source_issuer_id: str
) -> str:
    identity = {
        "source_id": source_id,
        "source_issuer_id": source_issuer_id,
        "year": request.year,
        "document_type": str(request.document_type),
        "source_document_type": request.source_document_type or "",
        "year_basis": str(request.year_basis),
        "include_amendments": request.include_amendments,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def amendment_like(source_id: str, form_raw: str, metadata: dict[str, Any]) -> bool:
    if bool(metadata.get("amendment")):
        return True
    normalized = normalize_source_document_type(form_raw)
    if source_id == "sec_edgar":
        return normalized.endswith("/a")
    if source_id == "edinet":
        return normalized in {"040", "070", "090", "130", "150", "170", "190"}
    title = str(metadata.get("title") or metadata.get("report_nm") or "")
    return any(
        marker in f"{form_raw} {title}"
        for marker in ("정정", "更正", "修订", "補足", "訂正")
    )


def _date_year(value: Any) -> int | None:
    if isinstance(value, (date, datetime)):
        return value.year
    match = re.match(r"^(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def _title_report_year(row: dict[str, Any]) -> int | None:
    metadata = row.get("metadata") or {}
    title = str(metadata.get("title") or row.get("title") or "")
    years = [int(value) for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", title)]
    return years[0] if years else None


def effective_year_basis(
    request: ResolveDisclosureRequest, source_id: str
) -> YearBasis:
    requested = YearBasis(request.year_basis)
    if requested != YearBasis.AUTO:
        return requested
    schema = get_source_schema(source_id)
    mapping = schema.mapping_for_canonical(CanonicalDocumentType(request.document_type))
    if mapping and mapping.year_semantics == "filing_date":
        return YearBasis.FILING_DATE
    return YearBasis.REPORT_PERIOD


def row_matches_request(
    row: dict[str, Any], request: ResolveDisclosureRequest
) -> bool:
    source_id = str(row["source_id"])
    schema = get_source_schema(source_id)
    form_raw = str(row.get("form_raw") or "")
    canonical_type = schema.canonicalize(form_raw)
    requested_type = CanonicalDocumentType(request.document_type)
    if canonical_type != requested_type:
        return False
    if request.source_document_type:
        native_mapping = schema.mapping_for_canonical(requested_type)
        exact_native_match = normalize_source_document_type(
            form_raw
        ) == normalize_source_document_type(request.source_document_type)
        semantic_native_match = bool(
            native_mapping
            and native_mapping.match != "exact"
            and native_mapping.matches(form_raw)
            and native_mapping.matches(request.source_document_type)
        )
        if not exact_native_match and not semantic_native_match:
            return False
    metadata = row.get("metadata") or {}
    if not request.include_amendments and amendment_like(
        source_id, form_raw, metadata
    ):
        return False

    basis = effective_year_basis(request, source_id)
    filing_year = _date_year(row.get("filing_date"))
    if basis == YearBasis.FILING_DATE:
        return filing_year == request.year

    report_year = _date_year(row.get("report_date")) or _title_report_year(row)
    if report_year is not None:
        return report_year == request.year
    # Some source indexes omit period_end. Periodic reports are commonly filed in
    # the report year or the following year; retain both and expose year_basis.
    return filing_year in {request.year, request.year + 1}


def select_local_documents(
    rows: list[dict[str, Any]], request: ResolveDisclosureRequest
) -> list[dict[str, Any]]:
    matches = [row for row in rows if row_matches_request(row, request)]
    matches.sort(
        key=lambda row: (
            str(row.get("report_date") or ""),
            str(row.get("filing_date") or ""),
            int(row.get("document_id") or 0),
        ),
        reverse=True,
    )
    return matches


def fallback_decision(source_id: str, allow_fallback: bool) -> tuple[bool, str]:
    schema = get_source_schema(source_id)
    if not allow_fallback:
        return False, "fallback_disabled"
    if schema.fallback_mode == FallbackMode.ON_DEMAND:
        return True, "queued"
    return False, schema.fallback_mode.value
