from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import EntityRef, SmokeResult, SmokeStatus

from gcu_priority_markets.io import alias_value, read_tabular_bytes


class KrxKindAdapter(BaseAdapter):
    """KRX KIND listed-company denominator. Filing acquisition reuses OpenDART."""

    UNIVERSE_URL = (
        "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
    )

    @classmethod
    def parse_universe(cls, content: bytes) -> Iterable[EntityRef]:
        # KIND serves an HTML table with an XLS filename and EUC-KR content.
        rows = read_tabular_bytes(
            content,
            "krx_company_list.xls",
            encodings=("euc-kr", "cp949", "utf-8-sig", "utf-8"),
        )
        for row in rows:
            code = str(
                alias_value(row, ["종목코드", "stock code", "code", "ticker"], "")
            ).strip()
            if code.endswith(".0") and code[:-2].isdigit():
                code = code[:-2]
            code = code.zfill(6) if code.isdigit() else code
            name = str(
                alias_value(row, ["회사명", "company name", "법인명", "name"], "")
            ).strip()
            market = str(
                alias_value(row, ["시장구분", "market", "시장", "market type"], "KRX")
            ).strip()
            if not code or not name:
                continue
            exchange = {
                "유가증권시장": "XKRX",
                "코스피": "XKRX",
                "KOSPI": "XKRX",
                "코스닥": "XKOS",
                "KOSDAQ": "XKOS",
                "코넥스": "XKON",
                "KONEX": "XKON",
            }.get(market, "XKRX")
            yield EntityRef(
                entity_id=f"krx-{code}",
                source_id="krx_kind",
                source_entity_id=code,
                legal_name=name,
                jurisdiction="KR",
                exchange=exchange,
                ticker=code,
                metadata={**row, "market_name": market},
            )

    def list_entities(self, **_: Any) -> Iterable[EntityRef]:
        response = self.client.request(
            "GET",
            self.UNIVERSE_URL,
            headers={
                "Referer": "https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage",
                "Accept": "application/vnd.ms-excel,text/html,*/*",
            },
        )
        for entity in self.parse_universe(response.content):
            entity.source_id = self.source_id
            yield entity

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        if offline:
            html = """
            <html><body><table>
              <tr><th>회사명</th><th>종목코드</th><th>업종</th><th>상장일</th></tr>
              <tr><td>삼성전자</td><td>005930</td><td>통신 및 방송 장비 제조업</td><td>1975-06-11</td></tr>
            </table></body></html>
            """.encode("utf-8")
            count = sum(1 for _ in self.parse_universe(html))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="listed_company_denominator",
                endpoint=self.UNIVERSE_URL,
                records_observed=count,
                message="KRX KIND HTML/XLS company-list contract validated offline; filings reuse OpenDART.",
            )
        try:
            response = self.client.request("GET", self.UNIVERSE_URL)
            count = sum(1 for _ in self.parse_universe(response.content))
            if count < 2_000:
                raise ValueError(
                    f"KRX KIND response produced only {count} rows; expected at least 2000"
                )
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="listed_company_denominator",
                endpoint=self.UNIVERSE_URL,
                records_observed=count,
                message="KRX KIND company list returned parseable rows.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("listed_company_denominator", self.UNIVERSE_URL, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="listed_company_denominator",
                endpoint=self.UNIVERSE_URL,
                message=f"KRX KIND smoke check failed: {type(exc).__name__}: {exc}",
            )
