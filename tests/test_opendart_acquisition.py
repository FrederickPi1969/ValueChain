from datetime import date

from valuechain.opendart_acquisition import OpenDartAcquisitionRunner
from valuechain.request_budget import RequestBudgetSnapshot


def test_request_budget_snapshot_never_reports_negative_remaining() -> None:
    snapshot = RequestBudgetSnapshot("opendart", "2026-07-14", 10_001, 10_000)

    assert snapshot.remaining == 0


def test_opendart_records_become_typed_filings_and_entities() -> None:
    entities, filings = OpenDartAcquisitionRunner._convert_records(
        [
            {
                "corp_code": "00126380",
                "corp_name": "삼성전자(주)",
                "stock_code": "005930",
                "corp_cls": "Y",
                "report_nm": "반기보고서 (2026.06)",
                "rcept_no": "20260714000001",
                "rcept_dt": "20260714",
            }
        ]
    )

    assert len(entities) == 1
    assert entities[0].source_entity_id == "00126380"
    assert entities[0].exchange == "XKRX"
    assert len(filings) == 1
    assert filings[0].filed_at == date(2026, 7, 14)
    assert filings[0].source_entity_id == "00126380"
    assert "crtfc_key" not in (filings[0].primary_document_url or "")
    assert "rcept_no=20260714000001" in (filings[0].primary_document_url or "")


def test_opendart_records_skip_rows_without_receipt_or_date() -> None:
    entities, filings = OpenDartAcquisitionRunner._convert_records(
        [{"corp_code": "00126380", "corp_name": "Samsung"}]
    )

    assert entities == []
    assert filings == []
