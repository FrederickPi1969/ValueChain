import pytest

from valuechain.disclosure_schema import (
    CanonicalDocumentType,
    FallbackMode,
    SOURCE_SCHEMAS,
    canonicalize_document_type,
    source_schema_catalog,
)


@pytest.mark.parametrize(
    ("source_id", "source_type", "expected"),
    [
        ("sec_edgar", "10-K", CanonicalDocumentType.ANNUAL_REPORT),
        ("sec_edgar", "20-F/A", CanonicalDocumentType.ANNUAL_REPORT),
        ("sec_edgar", "8-K", CanonicalDocumentType.CURRENT_REPORT),
        ("cninfo", "annual_report", CanonicalDocumentType.ANNUAL_REPORT),
        ("cninfo", "2025年半年度报告", CanonicalDocumentType.SEMIANNUAL_REPORT),
        ("cninfo", "q3_report", CanonicalDocumentType.QUARTERLY_REPORT),
        ("opendart", "사업보고서 (2025.12)", CanonicalDocumentType.ANNUAL_REPORT),
        ("edinet", "120", CanonicalDocumentType.ANNUAL_REPORT),
        ("cvm_brazil", "ITR", CanonicalDocumentType.INTERIM_FINANCIAL_STATEMENTS),
    ],
)
def test_source_document_types_map_to_canonical_taxonomy(
    source_id: str,
    source_type: str,
    expected: CanonicalDocumentType,
) -> None:
    assert canonicalize_document_type(source_id, source_type) == expected


def test_catalog_exposes_exact_names_identifiers_and_fallback_modes() -> None:
    catalog = {item["source_id"]: item for item in source_schema_catalog()}

    assert "cik" in catalog["sec_edgar"]["company_identifiers"]
    assert "10-K" in catalog["sec_edgar"]["mappings"][0]["source_names"]
    assert catalog["opendart"]["credential"] == "OPENDART_API_KEY"
    assert catalog["hkex"]["fallback_mode"] == "authorized_import_only"
    assert catalog["edinet"]["fallback_mode"] == "scheduled_bulk"


def test_only_legally_supported_connectors_claim_on_demand() -> None:
    assert SOURCE_SCHEMAS["sec_edgar"].fallback_mode == FallbackMode.ON_DEMAND
    assert SOURCE_SCHEMAS["cninfo"].fallback_mode == FallbackMode.ON_DEMAND
    assert SOURCE_SCHEMAS["opendart"].fallback_mode == FallbackMode.ON_DEMAND
    assert SOURCE_SCHEMAS["sedar_plus"].fallback_mode == FallbackMode.AUTHORIZED_IMPORT_ONLY
    assert SOURCE_SCHEMAS["asx"].fallback_mode == FallbackMode.AUTHORIZED_IMPORT_ONLY
