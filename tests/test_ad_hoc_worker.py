from datetime import date

from valuechain.ad_hoc_worker import _filing_window
from valuechain.disclosure_resolver import ResolveDisclosureRequest


def test_periodic_request_searches_report_year_and_next_filing_year() -> None:
    query = ResolveDisclosureRequest(
        source_id="sec_edgar",
        company="NVDA",
        year=2025,
        document_type="annual_report",
    )
    assert _filing_window(query) == (date(2025, 1, 1), date(2026, 12, 31))


def test_event_request_searches_only_filing_year() -> None:
    query = ResolveDisclosureRequest(
        source_id="sec_edgar",
        company="NVDA",
        year=2025,
        document_type="current_report",
    )
    assert _filing_window(query) == (date(2025, 1, 1), date(2025, 12, 31))
