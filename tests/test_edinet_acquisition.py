from datetime import date

from valuechain.curated_universe import CuratedCompany
from valuechain.edinet_acquisition import (
    EdinetAcquisitionRunner,
    normalize_edinet_ticker,
)


WATCHLIST = {
    "7203": CuratedCompany("7203", "Toyota Motor", 1, "mobility"),
}


def test_edinet_security_code_normalization() -> None:
    assert normalize_edinet_ticker("72030") == "7203"
    assert normalize_edinet_ticker("7203") == "7203"
    assert normalize_edinet_ticker(None) == ""


def test_edinet_selects_only_curated_high_value_issuer_filings() -> None:
    records = [
        {"secCode": "72030", "docTypeCode": "120", "withdrawalStatus": "0"},
        {"secCode": "72030", "docTypeCode": "135", "withdrawalStatus": "0"},
        {"secCode": "67580", "docTypeCode": "120", "withdrawalStatus": "0"},
        {"secCode": "72030", "docTypeCode": "180", "withdrawalStatus": "1"},
    ]

    selected = EdinetAcquisitionRunner._select_records(records, WATCHLIST)

    assert selected == [records[0]]


def test_edinet_records_become_typed_filings_and_entities() -> None:
    entities, filings = EdinetAcquisitionRunner._convert_records(
        [
            {
                "docID": "S100TEST",
                "edinetCode": "E02144",
                "secCode": "72030",
                "filerName": "トヨタ自動車株式会社",
                "docTypeCode": "120",
                "docDescription": "有価証券報告書",
                "submitDateTime": "2026-06-24 10:00",
                "periodEnd": "2026-03-31",
                "JCN": "1180301018771",
            }
        ],
        WATCHLIST,
    )

    assert len(entities) == 1
    assert entities[0].ticker == "7203"
    assert entities[0].metadata["curated_watchlist"] is True
    assert len(filings) == 1
    assert filings[0].filed_at == date(2026, 6, 24)
    assert filings[0].period_end == date(2026, 3, 31)
    assert "Subscription-Key" not in (filings[0].primary_document_url or "")
