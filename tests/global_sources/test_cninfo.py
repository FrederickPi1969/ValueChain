from __future__ import annotations

from datetime import date

from gcu.config import Settings
from gcu.models import EntityRef, SourceDefinition
from gcu_priority_markets.adapters.cninfo import CninfoAdapter


def test_cninfo_universe_parses_market_rows() -> None:
    payload = {
        "stockList": [
            {"code": "600519", "zwjc": "贵州茅台", "orgId": "gssh0600519"},
            {"code": "", "zwjc": "skip"},
        ]
    }
    rows = list(CninfoAdapter.parse_universe(payload, "SSE"))
    assert len(rows) == 1
    assert rows[0].ticker == "600519"
    assert rows[0].exchange == "XSHG"
    assert rows[0].source_entity_id == "gssh0600519"


def test_cninfo_announcements_parse_and_classify() -> None:
    payload = {
        "announcements": [
            {
                "announcementId": "1212345678",
                "secCode": "000001",
                "secName": "平安银行",
                "announcementTitle": "2025年年度报告（修订版）",
                "adjunctUrl": "finalpage/2026-03-01/report.pdf",
                "announcementTime": 1772323200000,
            }
        ]
    }
    filing = next(CninfoAdapter.parse_announcements(payload, market="SZSE"))
    assert filing.filing_id == "1212345678"
    assert filing.form == "annual_report"
    assert filing.amendment is True
    assert filing.primary_document_url.endswith("report.pdf")
    assert filing.metadata["issuer_name"] == "平安银行"


def test_cninfo_date_parser_accepts_iso_date() -> None:
    filed, moment = CninfoAdapter._date_from_millis("2026-07-10")
    assert filed == date(2026, 7, 10)
    assert moment is not None


def test_cninfo_combined_map_infers_all_three_markets() -> None:
    payload = {
        "stockList": [
            {"code": "600519", "zwjc": "贵州茅台", "orgId": "gssh0600519"},
            {"code": "000001", "zwjc": "平安银行", "orgId": "gssz0000001"},
            {"code": "920001", "zwjc": "北交示例", "orgId": "9900999999"},
        ]
    }
    rows = list(CninfoAdapter.parse_universe(payload))
    assert [row.exchange for row in rows] == ["XSHG", "XSHE", "XBSE"]


def test_cninfo_market_filter_does_not_duplicate_combined_map() -> None:
    payload = {
        "stockList": [
            {"code": "600519", "zwjc": "贵州茅台", "orgId": "gssh0600519"},
            {"code": "000001", "zwjc": "平安银行", "orgId": "gssz0000001"},
        ]
    }
    rows = list(CninfoAdapter.parse_universe(payload, "SSE"))
    assert len(rows) == 1
    assert rows[0].ticker == "600519"


def test_cninfo_rotates_proxy_after_partial_universe() -> None:
    partial = {"stockList": [{"code": "600519", "zwjc": "A", "orgId": "gssh1"}]}
    complete = {
        "stockList": [
            {"code": "600519", "zwjc": "A", "orgId": "gssh1"},
            {"code": "000001", "zwjc": "B", "orgId": "gssz1"},
        ]
    }

    class Client:
        def __init__(self) -> None:
            self.payloads = [partial, complete]
            self.rotations = 0
            self.rate_limiter = type("Limiter", (), {"set_host_rate": lambda *_: None})()

        def get_json(self, *_args, **_kwargs):
            return self.payloads.pop(0)

        def rotate_proxy(self) -> bool:
            self.rotations += 1
            return True

    client = Client()
    definition = SourceDefinition.model_validate(
        {
            "source_id": "cninfo",
            "name": "CNINFO",
            "jurisdictions": ["CN"],
            "adapter": "gcu_priority_markets.adapters.cninfo:CninfoAdapter",
            "access_mode": "semi_public_web_endpoint",
            "official_url": "https://www.cninfo.com.cn/",
        }
    )
    adapter = CninfoAdapter(definition=definition, settings=Settings(_env_file=None), client=client)
    adapter.MIN_UNIVERSE_ROWS = 2
    adapter.TARGET_UNIVERSE_ROWS = 2

    rows = list(adapter.list_entities())

    assert len(rows) == 2
    assert client.rotations == 1


def test_cninfo_maps_iso_mic_back_to_query_market() -> None:
    entity = EntityRef(
        entity_id="cninfo-a",
        source_id="cninfo",
        source_entity_id="gssh1",
        legal_name="Example",
        exchange="XSHG",
        ticker="600001",
    )

    assert CninfoAdapter.MIC_MARKETS[entity.exchange] == "SSE"
    assert "category_ndbg_szsh" in CninfoAdapter.FINANCIAL_REPORT_CATEGORIES
