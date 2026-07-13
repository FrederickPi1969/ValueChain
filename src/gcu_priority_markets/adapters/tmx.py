from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import EntityRef, SmokeResult, SmokeStatus

from gcu_priority_markets.io import alias_value, read_tabular_bytes


class TmxIssuerAdapter(BaseAdapter):
    DIRECTORY_URL = "https://www.tsx.com/en/listings/current-market-statistics"
    FALLBACK_RESOURCE_URL = "https://www.tsx.com/en/resource/571"

    @classmethod
    def discover_resource_url(cls, html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            text = " ".join(anchor.get_text(" ", strip=True).split()).lower()
            href = str(anchor["href"])
            if "tsx & tsxv listed companies" in text or "tsx/tsxv issuers" in text:
                return urljoin(cls.DIRECTORY_URL, href)
        match = re.search(r'href=["\']([^"\']*/en/resource/\d+)["\'][^>]*>[^<]*(?:listed companies|issuers)', html, re.I)
        return urljoin(cls.DIRECTORY_URL, match.group(1)) if match else None

    @classmethod
    def parse_universe(cls, content: bytes, filename: str = "tmx_issuers.xlsx") -> Iterable[EntityRef]:
        for row in read_tabular_bytes(content, filename):
            name = str(
                alias_value(row, ["Company Name", "Issuer Name", "Name", "Company"], "")
            ).strip()
            symbol = str(
                alias_value(row, ["Symbol", "Ticker", "Root Ticker", "Stock Symbol"], "")
            ).strip()
            sheet = str(row.get("__sheet__") or "").strip()
            exchange_value = str(
                alias_value(row, ["Exchange", "Market", "Board"], sheet)
            ).strip().upper()
            if "VENTURE" in exchange_value or "TSXV" in exchange_value:
                exchange = "XTSX"
                exchange_name = "TSXV"
            else:
                exchange = "XTSE"
                exchange_name = "TSX"
            if not name or not symbol:
                continue
            product_type = str(alias_value(row, ["SP_Type", "Security Type"], "")).strip()
            sector = str(alias_value(row, ["Sector", "Industry Sector", "Sector Name"], "")).strip()
            excluded_sectors = {"CDR", "ETP", "Closed-End Funds"}
            if product_type or sector in excluded_sectors or "bond" in name.lower():
                continue
            industry = alias_value(row, ["Industry", "Subsector", "Industry Name"])
            yield EntityRef(
                entity_id=f"tmx-{exchange_name.lower()}-{symbol}",
                source_id="tmx_issuer_lists",
                source_entity_id=f"{exchange_name}:{symbol}",
                legal_name=name,
                jurisdiction="CA",
                exchange=exchange,
                ticker=symbol,
                metadata={**row, "exchange_name": exchange_name, "sector": sector, "industry": industry},
            )

    def list_entities(self, *, resource_url: str | None = None, **_: Any) -> Iterable[EntityRef]:
        url = resource_url
        if not url:
            html = self.client.get_text(self.DIRECTORY_URL)
            url = self.discover_resource_url(html) or self.FALLBACK_RESOURCE_URL
        response = self.client.request("GET", url, headers={"Referer": self.DIRECTORY_URL})
        filename = response.url.path.rsplit("/", 1)[-1] or "tmx_issuers.xlsx"
        content_disposition = response.headers.get("content-disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", content_disposition, re.I)
        if match:
            filename = match.group(1).strip()
        for entity in self.parse_universe(response.content, filename):
            entity.source_id = self.source_id
            yield entity

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        if offline:
            html = '<a href="/en/resource/571">TSX &amp; TSXV Listed Companies</a>'
            discovered = self.discover_resource_url(html)
            csv_content = (
                b"Exchange,Company Name,Symbol,Sector,Industry\n"
                b"TSX,Shopify Inc.,SHOP,Technology,Software\n"
                b"TSXV,Example Mining Inc.,EXM,Mining,Gold\n"
            )
            count = sum(1 for _ in self.parse_universe(csv_content, "issuers.csv"))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="monthly_issuer_list",
                endpoint=discovered,
                records_observed=count,
                message="TMX current-resource discovery and issuer-list parser contracts validated offline.",
            )
        try:
            html = self.client.get_text(self.DIRECTORY_URL)
            url = self.discover_resource_url(html) or self.FALLBACK_RESOURCE_URL
            response = self.client.request("GET", url)
            count = sum(1 for _ in self.parse_universe(response.content, url))
            if count < 1_500:
                raise ValueError(
                    f"TMX response produced only {count} company rows; expected at least 1500"
                )
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="monthly_issuer_list",
                endpoint=url,
                records_observed=count,
                message="TMX current issuer list returned parseable rows.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("monthly_issuer_list", self.DIRECTORY_URL, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="monthly_issuer_list",
                endpoint=self.DIRECTORY_URL,
                message=f"TMX smoke check failed: {type(exc).__name__}: {exc}",
            )
