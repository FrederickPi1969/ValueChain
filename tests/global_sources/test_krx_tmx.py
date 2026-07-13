from __future__ import annotations

import io

import openpyxl

from gcu_priority_markets.adapters.krx import KrxKindAdapter
from gcu_priority_markets.adapters.tmx import TmxIssuerAdapter


def test_krx_kind_html_parse() -> None:
    content = """
    <table><tr><th>회사명</th><th>종목코드</th><th>시장구분</th></tr>
    <tr><td>삼성전자</td><td>005930</td><td>KOSPI</td></tr></table>
    """.encode("utf-8")
    entity = next(KrxKindAdapter.parse_universe(content))
    assert entity.ticker == "005930"
    assert entity.exchange == "XKRX"


def test_tmx_resource_discovery() -> None:
    html = '<a href="/en/resource/999">TSX &amp; TSXV Listed Companies</a>'
    assert TmxIssuerAdapter.discover_resource_url(html) == "https://www.tsx.com/en/resource/999"


def test_tmx_dual_market_parse() -> None:
    content = (
        b"Exchange,Company Name,Symbol,Sector,Industry\n"
        b"TSX,Shopify Inc.,SHOP,Technology,Software\n"
        b"TSX Venture,Example Mining Inc.,EXM,Mining,Gold\n"
    )
    rows = list(TmxIssuerAdapter.parse_universe(content, "issuers.csv"))
    assert [row.exchange for row in rows] == ["XTSE", "XTSX"]


def test_krx_kosdaq_mic() -> None:
    content = """
    <table><tr><th>회사명</th><th>종목코드</th><th>시장구분</th></tr>
    <tr><td>테스트</td><td>123456</td><td>KOSDAQ</td></tr></table>
    """.encode("utf-8")
    entity = next(KrxKindAdapter.parse_universe(content))
    assert entity.exchange == "XKOS"


def test_krx_konex_mic() -> None:
    content = """
    <table><tr><th>회사명</th><th>종목코드</th><th>시장구분</th></tr>
    <tr><td>테스트</td><td>654321</td><td>KONEX</td></tr></table>
    """.encode("utf-8")
    entity = next(KrxKindAdapter.parse_universe(content))
    assert entity.exchange == "XKON"


def test_krx_decodes_live_euc_kr_contract() -> None:
    text = """
    <table><tr><th>회사명</th><th>종목코드</th><th>시장구분</th></tr>
    <tr><td>삼성전자</td><td>005930</td><td>유가증권시장</td></tr></table>
    """
    entity = next(KrxKindAdapter.parse_universe(text.encode("euc-kr")))
    assert entity.legal_name == "삼성전자"
    assert entity.ticker == "005930"


def test_tmx_live_workbook_headers_and_product_filter() -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "TSX Issuers"
    sheet.append(["notice"])
    sheet.append(["Co_ID", "Exchange", "Name", "Root\nTicker", "Sector", "SP_Type"])
    sheet.append(["A1", "TSX", "Shopify Inc.", "SHOP", "Technology", None])
    sheet.append(["A2", "TSX", "Example ETF", "ETF", "ETP", "Exchange Traded Funds"])
    content = io.BytesIO()
    workbook.save(content)

    rows = list(TmxIssuerAdapter.parse_universe(content.getvalue(), "issuers.xlsx"))

    assert [(row.legal_name, row.ticker) for row in rows] == [("Shopify Inc.", "SHOP")]
