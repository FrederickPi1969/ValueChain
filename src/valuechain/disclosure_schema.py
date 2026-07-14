from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import re
from typing import Any


class CanonicalDocumentType(StrEnum):
    ANNUAL_REPORT = "annual_report"
    QUARTERLY_REPORT = "quarterly_report"
    SEMIANNUAL_REPORT = "semiannual_report"
    CURRENT_REPORT = "current_report"
    MATERIAL_EVENT = "material_event"
    EARNINGS_RELEASE = "earnings_release"
    ANNUAL_FINANCIAL_STATEMENTS = "annual_financial_statements"
    INTERIM_FINANCIAL_STATEMENTS = "interim_financial_statements"
    REFERENCE_FORM = "reference_form"
    REGISTRATION_STATEMENT = "registration_statement"
    OTHER_REGULATORY_FILING = "other_regulatory_filing"


class FallbackMode(StrEnum):
    ON_DEMAND = "on_demand"
    SCHEDULED_BULK = "scheduled_bulk"
    CURRENT_ONLY = "current_only"
    AUTHORIZED_IMPORT_ONLY = "authorized_import_only"
    LOCAL_ONLY = "local_only"


@dataclass(frozen=True)
class DocumentMapping:
    canonical_type: CanonicalDocumentType
    source_names: tuple[str, ...]
    match: str = "exact"
    year_semantics: str = "report_period"
    notes: str = ""

    def matches(self, value: str) -> bool:
        normalized = normalize_source_document_type(value)
        names = tuple(normalize_source_document_type(name) for name in self.source_names)
        if self.match == "exact":
            return normalized in names
        if self.match == "contains":
            return any(name in normalized for name in names)
        if self.match == "prefix":
            return any(normalized.startswith(name) for name in names)
        raise ValueError(f"Unknown document mapping match mode: {self.match}")


@dataclass(frozen=True)
class SourceDisclosureSchema:
    source_id: str
    authority: str
    jurisdictions: tuple[str, ...]
    company_identifiers: tuple[str, ...]
    fallback_mode: FallbackMode
    credential: str | None
    official_url: str
    mappings: tuple[DocumentMapping, ...]
    fallback_notes: str

    @property
    def canonical_types(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(mapping.canonical_type.value for mapping in self.mappings))

    def mapping_for_canonical(
        self, document_type: CanonicalDocumentType
    ) -> DocumentMapping | None:
        return next(
            (mapping for mapping in self.mappings if mapping.canonical_type == document_type),
            None,
        )

    def canonicalize(self, source_document_type: str) -> CanonicalDocumentType:
        for mapping in self.mappings:
            if mapping.matches(source_document_type):
                return mapping.canonical_type
        return CanonicalDocumentType.OTHER_REGULATORY_FILING

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["fallback_mode"] = self.fallback_mode.value
        payload["canonical_types"] = list(self.canonical_types)
        for mapping in payload["mappings"]:
            mapping["canonical_type"] = mapping["canonical_type"].value
        return payload


def mapping(
    canonical_type: CanonicalDocumentType,
    *source_names: str,
    match: str = "exact",
    year_semantics: str = "report_period",
    notes: str = "",
) -> DocumentMapping:
    return DocumentMapping(
        canonical_type=canonical_type,
        source_names=tuple(source_names),
        match=match,
        year_semantics=year_semantics,
        notes=notes,
    )


SOURCE_SCHEMAS: dict[str, SourceDisclosureSchema] = {
    "sec_edgar": SourceDisclosureSchema(
        source_id="sec_edgar",
        authority="U.S. Securities and Exchange Commission",
        jurisdictions=("US",),
        company_identifiers=("cik", "ticker", "legal_name"),
        fallback_mode=FallbackMode.ON_DEMAND,
        credential=None,
        official_url="https://www.sec.gov/edgar/",
        mappings=(
            mapping(CanonicalDocumentType.ANNUAL_REPORT, "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"),
            mapping(CanonicalDocumentType.QUARTERLY_REPORT, "10-Q", "10-Q/A"),
            mapping(
                CanonicalDocumentType.CURRENT_REPORT,
                "8-K", "8-K/A", "6-K", "6-K/A",
                year_semantics="filing_date",
            ),
        ),
        fallback_notes="Public submissions API discovery plus official EDGAR archive download. Earnings releases are normally inside 8-K/6-K packages and are not represented as a distinct SEC form.",
    ),
    "cninfo": SourceDisclosureSchema(
        source_id="cninfo",
        authority="CNINFO / Shenzhen Stock Exchange",
        jurisdictions=("CN",),
        company_identifiers=("org_id", "ticker", "legal_name"),
        fallback_mode=FallbackMode.ON_DEMAND,
        credential=None,
        official_url="https://www.cninfo.com.cn/",
        mappings=(
            mapping(CanonicalDocumentType.SEMIANNUAL_REPORT, "semiannual_report", "半年度报告", match="contains"),
            mapping(CanonicalDocumentType.QUARTERLY_REPORT, "quarterly_report", "q1_report", "q3_report", "季度报告", match="contains"),
            mapping(CanonicalDocumentType.ANNUAL_REPORT, "annual_report", "年度报告", match="contains"),
        ),
        fallback_notes="Official disclosure search endpoint queried by security and filing date. Summary/abstract documents are excluded.",
    ),
    "opendart": SourceDisclosureSchema(
        source_id="opendart",
        authority="Financial Supervisory Service Korea",
        jurisdictions=("KR",),
        company_identifiers=("corp_code", "stock_code", "legal_name"),
        fallback_mode=FallbackMode.ON_DEMAND,
        credential="OPENDART_API_KEY",
        official_url="https://opendart.fss.or.kr/",
        mappings=(
            mapping(CanonicalDocumentType.ANNUAL_REPORT, "사업보고서", match="contains"),
            mapping(CanonicalDocumentType.SEMIANNUAL_REPORT, "반기보고서", match="contains"),
            mapping(CanonicalDocumentType.QUARTERLY_REPORT, "분기보고서", match="contains"),
            mapping(
                CanonicalDocumentType.MATERIAL_EVENT,
                "주요사항보고서", match="contains", year_semantics="filing_date",
            ),
        ),
        fallback_notes="OpenDART list API queried by corp_code and date range; original disclosure ZIP is retained. Daily API budget is enforced.",
    ),
    "edinet": SourceDisclosureSchema(
        source_id="edinet",
        authority="Financial Services Agency Japan",
        jurisdictions=("JP",),
        company_identifiers=("edinet_code", "security_code", "legal_name"),
        fallback_mode=FallbackMode.SCHEDULED_BULK,
        credential="EDINET_API_KEY",
        official_url="https://disclosure2.edinet-fsa.go.jp/",
        mappings=(
            mapping(CanonicalDocumentType.REGISTRATION_STATEMENT, "030", "040", "060", "070", "080", "090"),
            mapping(CanonicalDocumentType.ANNUAL_REPORT, "120", "130"),
            mapping(CanonicalDocumentType.QUARTERLY_REPORT, "140", "150"),
            mapping(CanonicalDocumentType.SEMIANNUAL_REPORT, "160", "170"),
            mapping(CanonicalDocumentType.MATERIAL_EVENT, "180", "190", year_semantics="filing_date"),
        ),
        fallback_notes="EDINET v2 lists the whole market one date at a time. Historical single-company misses are queued for scheduled date-index coverage instead of consuming hundreds of calls synchronously.",
    ),
    "priority_eu_esef": SourceDisclosureSchema(
        source_id="priority_eu_esef",
        authority="National OAMs via filings.xbrl.org discovery",
        jurisdictions=("EEA", "GB"),
        company_identifiers=("lei", "legal_name"),
        fallback_mode=FallbackMode.SCHEDULED_BULK,
        credential=None,
        official_url="https://filings.xbrl.org/",
        mappings=(mapping(CanonicalDocumentType.ANNUAL_REPORT, "ESEF", "iXBRL", "annual", match="contains"),),
        fallback_notes="Useful ESEF package discovery layer, but not a canonical completeness source. Scheduled country scans are used.",
    ),
    "twse": SourceDisclosureSchema(
        source_id="twse",
        authority="Taiwan Stock Exchange",
        jurisdictions=("TW",),
        company_identifiers=("ticker", "legal_name"),
        fallback_mode=FallbackMode.CURRENT_ONLY,
        credential=None,
        official_url="https://openapi.twse.com.tw/",
        mappings=(mapping(CanonicalDocumentType.MATERIAL_EVENT, "material_event", year_semantics="filing_date"),),
        fallback_notes="Current material-event OpenAPI only. Historical report packages require an authorized MOPS delivery.",
    ),
    "tpex": SourceDisclosureSchema(
        source_id="tpex",
        authority="Taipei Exchange",
        jurisdictions=("TW",),
        company_identifiers=("ticker", "legal_name"),
        fallback_mode=FallbackMode.CURRENT_ONLY,
        credential=None,
        official_url="https://www.tpex.org.tw/openapi/",
        mappings=(mapping(CanonicalDocumentType.MATERIAL_EVENT, "material_event", year_semantics="filing_date"),),
        fallback_notes="Current material-event OpenAPI only. Historical report packages require an authorized MOPS delivery.",
    ),
    "cvm_brazil": SourceDisclosureSchema(
        source_id="cvm_brazil",
        authority="Comissao de Valores Mobiliarios",
        jurisdictions=("BR",),
        company_identifiers=("cvm_code", "cnpj", "ticker", "legal_name"),
        fallback_mode=FallbackMode.SCHEDULED_BULK,
        credential=None,
        official_url="https://dados.cvm.gov.br/",
        mappings=(
            mapping(CanonicalDocumentType.ANNUAL_FINANCIAL_STATEMENTS, "DFP"),
            mapping(CanonicalDocumentType.INTERIM_FINANCIAL_STATEMENTS, "ITR"),
            mapping(CanonicalDocumentType.REFERENCE_FORM, "FRE"),
            mapping(CanonicalDocumentType.MATERIAL_EVENT, "IPE", year_semantics="filing_date"),
        ),
        fallback_notes="Official year-level ZIP archives are collected as scheduled bulk objects; they are not company-level ad hoc downloads.",
    ),
    "companies_house_accounts_bulk": SourceDisclosureSchema(
        source_id="companies_house_accounts_bulk",
        authority="UK Companies House",
        jurisdictions=("GB",),
        company_identifiers=("company_number", "legal_name"),
        fallback_mode=FallbackMode.SCHEDULED_BULK,
        credential=None,
        official_url="https://download.companieshouse.gov.uk/en_accountsdata.html",
        mappings=(mapping(CanonicalDocumentType.ANNUAL_FINANCIAL_STATEMENTS, "accounts"),),
        fallback_notes="Daily accounts ZIP snapshots are retained as bulk objects. Company-level API fallback is not enabled until an operator credential and listed-company mapping are configured.",
    ),
    "hkex": SourceDisclosureSchema(
        source_id="hkex",
        authority="Hong Kong Exchanges and Clearing",
        jurisdictions=("HK",),
        company_identifiers=("stock_code", "legal_name"),
        fallback_mode=FallbackMode.AUTHORIZED_IMPORT_ONLY,
        credential="authorized HKEX delivery",
        official_url="https://www.hkexnews.hk/",
        mappings=(
            mapping(CanonicalDocumentType.ANNUAL_REPORT, "Annual Report", match="contains"),
            mapping(CanonicalDocumentType.INTERIM_FINANCIAL_STATEMENTS, "Interim Report", match="contains"),
            mapping(CanonicalDocumentType.QUARTERLY_REPORT, "Quarterly Report", match="contains"),
            mapping(CanonicalDocumentType.MATERIAL_EVENT, "Announcement", match="contains", year_semantics="filing_date"),
        ),
        fallback_notes="Public-site systematic retrieval is not used. Only operator-authorized exports or feeds are imported.",
    ),
    "sedar_plus": SourceDisclosureSchema(
        source_id="sedar_plus",
        authority="Canadian Securities Administrators",
        jurisdictions=("CA",),
        company_identifiers=("profile_number", "ticker", "legal_name"),
        fallback_mode=FallbackMode.AUTHORIZED_IMPORT_ONLY,
        credential="licensed or negotiated SEDAR+ delivery",
        official_url="https://www.sedarplus.ca/",
        mappings=(
            mapping(CanonicalDocumentType.ANNUAL_FINANCIAL_STATEMENTS, "Annual financial statements", match="contains"),
            mapping(CanonicalDocumentType.INTERIM_FINANCIAL_STATEMENTS, "Interim financial report", match="contains"),
            mapping(CanonicalDocumentType.ANNUAL_REPORT, "Annual information form", match="contains"),
            mapping(CanonicalDocumentType.MATERIAL_EVENT, "Material change report", match="contains", year_semantics="filing_date"),
        ),
        fallback_notes="SEDAR+ public terms do not permit the planned scraping/database workflow. Authorized packages only.",
    ),
    "asx": SourceDisclosureSchema(
        source_id="asx",
        authority="Australian Securities Exchange",
        jurisdictions=("AU",),
        company_identifiers=("ticker", "legal_name"),
        fallback_mode=FallbackMode.AUTHORIZED_IMPORT_ONLY,
        credential="ASX ComNews entitlement",
        official_url="https://www.asx.com.au/connectivity-and-data/information-services/company-news",
        mappings=(
            mapping(CanonicalDocumentType.ANNUAL_REPORT, "Annual Report", match="contains"),
            mapping(CanonicalDocumentType.INTERIM_FINANCIAL_STATEMENTS, "Half Yearly Report", match="contains"),
            mapping(CanonicalDocumentType.MATERIAL_EVENT, "Market Announcement", match="contains", year_semantics="filing_date"),
        ),
        fallback_notes="ASX ComNews or written authorization is required for systematic collection and storage.",
    ),
    "mops": SourceDisclosureSchema(
        source_id="mops",
        authority="Taiwan Stock Exchange MOPS",
        jurisdictions=("TW",),
        company_identifiers=("ticker", "legal_name"),
        fallback_mode=FallbackMode.AUTHORIZED_IMPORT_ONLY,
        credential="MOPS Push Server/Data E-Shop entitlement",
        official_url="https://mops.twse.com.tw/",
        mappings=(
            mapping(CanonicalDocumentType.ANNUAL_REPORT, "annual_report"),
            mapping(CanonicalDocumentType.QUARTERLY_REPORT, "quarterly_report"),
            mapping(CanonicalDocumentType.MATERIAL_EVENT, "material_event", year_semantics="filing_date"),
        ),
        fallback_notes="Historical disclosure packages are accepted only from an authorized delivery.",
    ),
    "unternehmensregister": SourceDisclosureSchema(
        source_id="unternehmensregister",
        authority="German Company Register",
        jurisdictions=("DE",),
        company_identifiers=("lei", "register_id", "legal_name"),
        fallback_mode=FallbackMode.AUTHORIZED_IMPORT_ONLY,
        credential="approved export or bulk delivery",
        official_url="https://www.unternehmensregister.de/",
        mappings=(mapping(CanonicalDocumentType.ANNUAL_REPORT, "ESEF annual report", match="contains"),),
        fallback_notes="No public unattended bulk API is documented; CAPTCHA/security controls are not bypassed.",
    ),
}


def normalize_source_document_type(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def get_source_schema(source_id: str) -> SourceDisclosureSchema:
    try:
        return SOURCE_SCHEMAS[source_id]
    except KeyError as exc:
        raise ValueError(f"Unknown disclosure source: {source_id}") from exc


def canonicalize_document_type(
    source_id: str, source_document_type: str
) -> CanonicalDocumentType:
    return get_source_schema(source_id).canonicalize(source_document_type)


def source_schema_catalog() -> list[dict[str, Any]]:
    return [SOURCE_SCHEMAS[source_id].public_dict() for source_id in sorted(SOURCE_SCHEMAS)]
