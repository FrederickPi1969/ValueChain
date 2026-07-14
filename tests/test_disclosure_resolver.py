from datetime import date

import pytest
from pydantic import ValidationError

from valuechain.disclosure_resolver import (
    ResolveDisclosureRequest,
    fallback_decision,
    request_key,
    select_local_documents,
)


def request(**overrides):
    values = {
        "source_id": "sec_edgar",
        "company": "NVDA",
        "year": 2025,
        "document_type": "annual_report",
    }
    values.update(overrides)
    return ResolveDisclosureRequest.model_validate(values)


def test_request_rejects_native_type_from_wrong_canonical_family() -> None:
    with pytest.raises(ValidationError):
        request(source_document_type="10-Q")


def test_report_period_matches_next_year_filing_and_excludes_amendment() -> None:
    rows = [
        {
            "document_id": 1,
            "source_id": "sec_edgar",
            "form_raw": "10-K",
            "filing_date": date(2026, 2, 20),
            "report_date": "2025-12-31",
            "metadata": {},
        },
        {
            "document_id": 2,
            "source_id": "sec_edgar",
            "form_raw": "10-K/A",
            "filing_date": date(2026, 3, 1),
            "report_date": "2025-12-31",
            "metadata": {},
        },
        {
            "document_id": 3,
            "source_id": "sec_edgar",
            "form_raw": "10-K",
            "filing_date": date(2025, 2, 20),
            "report_date": "2024-12-31",
            "metadata": {},
        },
    ]

    assert [row["document_id"] for row in select_local_documents(rows, request())] == [1]


def test_event_document_uses_filing_year() -> None:
    query = request(document_type="current_report", year=2025)
    rows = [
        {
            "document_id": 7,
            "source_id": "sec_edgar",
            "form_raw": "8-K",
            "filing_date": date(2025, 8, 1),
            "report_date": "2024-12-31",
            "metadata": {},
        }
    ]
    assert select_local_documents(rows, query) == rows


def test_contains_mapping_accepts_native_label_but_sec_form_stays_exact() -> None:
    cn_query = ResolveDisclosureRequest(
        source_id="cninfo",
        company="000001",
        year=2025,
        document_type="annual_report",
        source_document_type="年度报告",
    )
    cn_row = {
        "document_id": 8,
        "source_id": "cninfo",
        "form_raw": "annual_report",
        "filing_date": date(2026, 3, 1),
        "report_date": "",
        "metadata": {"title": "平安银行2025年年度报告"},
    }
    assert select_local_documents([cn_row], cn_query) == [cn_row]

    sec_query = request(source_document_type="20-F")
    sec_row = {
        "document_id": 9,
        "source_id": "sec_edgar",
        "form_raw": "10-K",
        "filing_date": date(2026, 3, 1),
        "report_date": "2025-12-31",
        "metadata": {},
    }
    assert select_local_documents([sec_row], sec_query) == []


def test_cninfo_correction_is_excluded_from_default_results() -> None:
    row = {
        "document_id": 10,
        "source_id": "cninfo",
        "form_raw": "annual_report",
        "filing_date": date(2026, 4, 1),
        "report_date": "",
        "metadata": {"title": "2025年年度报告（修订版）"},
    }
    assert select_local_documents([row], ResolveDisclosureRequest(
        source_id="cninfo",
        company="000001",
        year=2025,
        document_type="annual_report",
    )) == []


def test_request_key_deduplicates_same_resolved_company_request() -> None:
    query = request()
    assert request_key(query, "sec_edgar", "0001045810") == request_key(
        query, "sec_edgar", "0001045810"
    )
    assert request_key(query, "sec_edgar", "0001045810") != request_key(
        query, "sec_edgar", "0000320193"
    )


def test_fallback_decision_exposes_scheduled_and_authorized_lanes() -> None:
    assert fallback_decision("sec_edgar", True) == (True, "queued")
    assert fallback_decision("edinet", True) == (False, "scheduled_bulk")
    assert fallback_decision("hkex", True) == (False, "authorized_import_only")
    assert fallback_decision("sec_edgar", False) == (False, "fallback_disabled")
