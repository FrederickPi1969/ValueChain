from __future__ import annotations

from gcu_priority_markets.adapters.india import BseIndiaAdapter, NseIndiaAdapter


def test_nse_universe_csv() -> None:
    content = (
        b"SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,ISIN NUMBER,FACE VALUE\n"
        b"RELIANCE,Reliance Industries Limited,EQ,29-Nov-1995,INE002A01018,10\n"
    )
    entity = next(NseIndiaAdapter.parse_universe(content))
    assert entity.ticker == "RELIANCE"
    assert entity.isin == "INE002A01018"
    assert entity.exchange == "XNSE"


def test_nse_announcements_document_url() -> None:
    payload = [
        {
            "symbol": "INFY",
            "sm_name": "Infosys Limited",
            "desc": "Financial Results",
            "an_dt": "10-Jul-2026 18:30:00",
            "attchmntFile": "https://nsearchives.nseindia.com/corporate/INFY_10072026.pdf",
            "seq_id": "1234",
        }
    ]
    filing = next(NseIndiaAdapter.parse_announcements(payload))
    assert filing.source_entity_id == "INFY"
    assert filing.primary_document_url.endswith(".pdf")
    assert filing.filed_at.isoformat() == "2026-07-10"


def test_bse_universe_csv() -> None:
    content = b"Security Code,Security Name,Status,Group,ISIN No\n500325,RELIANCE,Active,A,INE002A01018\n"
    entity = next(BseIndiaAdapter.parse_universe(content))
    assert entity.ticker == "500325"
    assert entity.exchange == "XBOM"


def test_bse_announcements_parse() -> None:
    payload = {
        "Table": [
            {
                "NEWSID": "news-1",
                "SCRIP_CD": "500325",
                "SLONGNAME": "Reliance Industries Ltd",
                "HEADLINE": "Board Meeting Outcome",
                "NEWS_DT": "2026-07-10T18:00:00",
                "ATTACHMENTNAME": "abc.pdf",
            }
        ]
    }
    filing = next(BseIndiaAdapter.parse_announcements(payload))
    assert filing.filing_id == "news-1"
    assert filing.primary_document_url.endswith("abc.pdf")
