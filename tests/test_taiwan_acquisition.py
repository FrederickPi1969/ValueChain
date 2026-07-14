from datetime import date

from valuechain.taiwan_acquisition import (
    SOURCE_CONTRACTS,
    event_identifier,
    events_to_filings,
    parse_roc_date,
)


def test_roc_and_gregorian_dates_are_normalized() -> None:
    assert parse_roc_date("115/07/14") == date(2026, 7, 14)
    assert parse_roc_date("1150714") == date(2026, 7, 14)
    assert parse_roc_date("20260714") == date(2026, 7, 14)
    assert parse_roc_date("invalid") is None


def test_material_event_becomes_evidence_backed_filing() -> None:
    row = {
        "公司代號": "2330",
        "公司名稱": "台灣積體電路製造股份有限公司",
        "發言日期": "1150714",
        "發言時間": "101530",
        "事實發生日": "1150713",
        "主旨": "取得先進製程設備",
        "說明": "向供應商取得設備以擴充產能。",
    }

    filing = events_to_filings("twse", [row], "https://example.test/events")[0]

    assert filing.source_entity_id == "2330"
    assert filing.form == "material_event"
    assert filing.filed_at == date(2026, 7, 14)
    assert "供應商" in filing.metadata["evidence_text"]
    assert filing.detail_url and filing.filing_id in filing.detail_url


def test_material_event_identifier_is_stable_and_source_scoped() -> None:
    row = {"公司代號": "2330", "發言日期": "1150714", "主旨": "重大訊息"}
    assert event_identifier("twse", row) == event_identifier("twse", dict(row))
    assert event_identifier("twse", row) != event_identifier("tpex", row)


def test_each_taiwan_market_captures_all_financial_industry_shapes() -> None:
    assert len(SOURCE_CONTRACTS["twse"].financial_urls) == 12
    assert len(SOURCE_CONTRACTS["tpex"].financial_urls) == 12
    assert SOURCE_CONTRACTS["twse"].event_url.endswith("t187ap04_L")
    assert SOURCE_CONTRACTS["tpex"].event_url.endswith("mopsfin_t187ap04_O")
