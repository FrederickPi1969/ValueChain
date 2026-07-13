from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import DocumentRef, EntityRef, FilingRef, SmokeResult, SmokeStatus

from gcu_priority_markets.io import alias_value, read_tabular_bytes


def _parse_date(value: Any) -> tuple[date | None, datetime | None]:
    if value in (None, ""):
        return None, None
    text = str(value).strip()
    formats = (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=UTC)
            return parsed.date(), parsed
        except ValueError:
            continue
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.date(), parsed
    except (TypeError, ValueError, OverflowError):
        return None, None


def _filing_form(title: str) -> str:
    lowered = title.lower()
    mapping = (
        (("annual report", "annual financial"), "annual_report"),
        (("quarterly", "quarter ended", "financial results"), "financial_results"),
        (("shareholding pattern",), "shareholding_pattern"),
        (("board meeting",), "board_meeting"),
        (("investor presentation",), "investor_presentation"),
        (("press release",), "press_release"),
    )
    for markers, form in mapping:
        if any(marker in lowered for marker in markers):
            return form
    return "corporate_announcement"


class NseIndiaAdapter(BaseAdapter):
    MAIN_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    SME_URL = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
    BOOTSTRAP_URL = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
    FILING_URL = "https://www.nseindia.com/api/corporate-announcements"

    @classmethod
    def parse_universe(cls, content: bytes, exchange: str = "XNSE") -> Iterable[EntityRef]:
        for row in read_tabular_bytes(content, "EQUITY_L.csv"):
            symbol = str(alias_value(row, ["SYMBOL", "symbol", "security symbol"], "")).strip()
            name = str(alias_value(row, ["NAME OF COMPANY", "company name", "name"], "")).strip()
            isin = str(alias_value(row, ["ISIN NUMBER", "isin", "isin no"], "")).strip() or None
            series = str(alias_value(row, ["SERIES", "series"], "")).strip()
            if not symbol or not name:
                continue
            yield EntityRef(
                entity_id=f"nse-{symbol}",
                source_id="nse_india",
                source_entity_id=symbol,
                legal_name=name,
                jurisdiction="IN",
                exchange=exchange,
                ticker=symbol,
                isin=isin,
                metadata={**row, "series": series, "market": exchange},
            )

    def list_entities(
        self,
        *,
        include_main: bool = True,
        include_sme: bool = True,
        **_: Any,
    ) -> Iterable[EntityRef]:
        headers = {"Referer": "https://www.nseindia.com/market-data/securities-available-for-trading"}
        if include_main:
            response = self.client.request("GET", self.MAIN_URL, headers=headers)
            for entity in self.parse_universe(response.content, "XNSE"):
                entity.source_id = self.source_id
                yield entity
        if include_sme:
            response = self.client.request("GET", self.SME_URL, headers=headers)
            for entity in self.parse_universe(response.content, "XNSE_SME"):
                entity.source_id = self.source_id
                entity.entity_id = f"nse-sme-{entity.source_entity_id}"
                yield entity

    @staticmethod
    def _attachment_url(row: dict[str, Any]) -> str | None:
        for key in ("attchmntFile", "attachment", "attachmentFile", "fileName", "url"):
            value = row.get(key)
            if not value:
                continue
            text = str(value).strip()
            if text.startswith("http"):
                return text
            if text.startswith("/"):
                return "https://www.nseindia.com" + text
            return "https://nsearchives.nseindia.com/corporate/" + text.lstrip("/")
        return None

    @classmethod
    def parse_announcements(
        cls,
        payload: Any,
        *,
        entity: EntityRef | None = None,
    ) -> Iterable[FilingRef]:
        if isinstance(payload, dict):
            rows = payload.get("data") or payload.get("records") or payload.get("rows") or []
        else:
            rows = payload if isinstance(payload, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or row.get("sm_symbol") or row.get("security") or "").strip()
            name = str(row.get("sm_name") or row.get("companyName") or row.get("company") or "").strip()
            title = str(row.get("desc") or row.get("subject") or row.get("headline") or "").strip()
            filed_at, published_at = _parse_date(
                row.get("an_dt") or row.get("sort_date") or row.get("broadcastDate") or row.get("date")
            )
            attachment = cls._attachment_url(row)
            sequence = str(
                row.get("seq_id")
                or row.get("sequenceId")
                or row.get("announcementId")
                or ""
            ).strip()
            if not sequence:
                digest = hashlib.sha256(
                    f"{symbol}|{published_at}|{title}|{attachment}".encode("utf-8")
                ).hexdigest()[:24]
                sequence = digest
            source_entity_id = entity.source_entity_id if entity else symbol
            entity_id = entity.entity_id if entity else f"nse-{symbol or sequence}"
            yield FilingRef(
                source_id="nse_india",
                filing_id=sequence,
                entity_id=entity_id,
                source_entity_id=source_entity_id or None,
                form=_filing_form(title),
                title=title or sequence,
                filed_at=filed_at,
                detail_url=str(row.get("announcementLink") or row.get("detailUrl") or "") or None,
                primary_document_url=attachment,
                language="en",
                amendment=any(token in title.lower() for token in ("revised", "corrigendum", "clarification")),
                metadata={
                    **row,
                    "security_code": symbol,
                    "issuer_name": name,
                    "published_at": published_at.isoformat() if published_at else None,
                },
            )

    def _bootstrap(self) -> None:
        self.client.request(
            "GET",
            self.BOOTSTRAP_URL,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def list_recent_filings(
        self,
        *,
        begin: date,
        end: date,
        entity: EntityRef | None = None,
        index: str = "equities",
        **_: Any,
    ) -> Iterable[FilingRef]:
        self._bootstrap()
        params: dict[str, Any] = {
            "index": index,
            "from_date": begin.strftime("%d-%m-%Y"),
            "to_date": end.strftime("%d-%m-%Y"),
        }
        if entity and entity.ticker:
            params["symbol"] = entity.ticker
        payload = self.client.get_json(
            self.FILING_URL,
            params=params,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": self.BOOTSTRAP_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        for filing in self.parse_announcements(payload, entity=entity):
            filing.source_id = self.source_id
            yield filing

    def list_filings(
        self,
        entity: EntityRef,
        *,
        begin: date,
        end: date,
        **kwargs: Any,
    ) -> Iterable[FilingRef]:
        yield from self.list_recent_filings(begin=begin, end=end, entity=entity, **kwargs)

    def list_documents(self, filing: FilingRef, **_: Any) -> Iterable[DocumentRef]:
        if filing.primary_document_url:
            filename = filing.primary_document_url.split("?", 1)[0].rsplit("/", 1)[-1]
            yield DocumentRef(
                source_id=self.source_id,
                document_id=f"{filing.filing_id}:attachment",
                filing_id=filing.filing_id,
                entity_id=filing.entity_id,
                url=filing.primary_document_url,
                filename=filename or f"{filing.filing_id}.bin",
                document_type=filing.form,
                filed_at=filing.filed_at,
            )

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        if offline:
            content = b"SYMBOL,NAME OF COMPANY,SERIES,ISIN NUMBER\nRELIANCE,Reliance Industries Limited,EQ,INE002A01018\n"
            entities = list(self.parse_universe(content))
            filings = list(
                self.parse_announcements(
                    [
                        {
                            "symbol": "RELIANCE",
                            "sm_name": "Reliance Industries Limited",
                            "desc": "Financial Results for quarter ended",
                            "an_dt": "10-Jul-2026 17:30:00",
                            "seq_id": "12345",
                            "attchmntFile": "RELIANCE_10072026.pdf",
                        }
                    ]
                )
            )
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="universe_and_corporate_announcements",
                endpoint=self.MAIN_URL,
                records_observed=len(entities) + len(filings),
                message="NSE security-master and corporate-announcement contracts validated offline.",
            )
        try:
            response = self.client.request("GET", self.MAIN_URL)
            count = sum(1 for _ in self.parse_universe(response.content))
            if count < 1_000:
                raise ValueError(
                    f"NSE response produced only {count} rows; expected at least 1000"
                )
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="security_master",
                endpoint=self.MAIN_URL,
                records_observed=count,
                message="NSE security master returned parseable rows.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("security_master", self.MAIN_URL, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="security_master",
                endpoint=self.MAIN_URL,
                message=f"NSE smoke check failed: {type(exc).__name__}: {exc}",
            )


class BseIndiaAdapter(BaseAdapter):
    UNIVERSE_URL = "https://www.bseindia.com/downloads1/List_of_companies.csv"
    FILING_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
    ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
    REFERER = "https://www.bseindia.com/corporates/ann.html"

    @classmethod
    def parse_universe(cls, content: bytes) -> Iterable[EntityRef]:
        for row in read_tabular_bytes(content, "List_of_companies.csv"):
            code = str(
                alias_value(row, ["Security Code", "scrip code", "scripcode", "code"], "")
            ).strip()
            symbol = str(
                alias_value(row, ["Security Id", "security id", "scrip id", "symbol"], code)
            ).strip()
            name = str(
                alias_value(row, ["Issuer Name", "Security Name", "company name", "name"], "")
            ).strip()
            isin = str(alias_value(row, ["ISIN No", "ISIN", "isin number"], "")).strip() or None
            status = str(alias_value(row, ["Status", "status"], "")).strip()
            if not code or not name:
                continue
            if status and status.lower() not in {"active", "a", "listed"}:
                continue
            yield EntityRef(
                entity_id=f"bse-{code}",
                source_id="bse_india",
                source_entity_id=code,
                legal_name=name,
                jurisdiction="IN",
                exchange="XBOM",
                ticker=symbol or code,
                isin=isin,
                metadata=row,
            )

    def list_entities(self, **_: Any) -> Iterable[EntityRef]:
        response = self.client.request(
            "GET",
            self.UNIVERSE_URL,
            headers={"Referer": "https://www.bseindia.com/corporates/List_Scrips.html"},
        )
        for entity in self.parse_universe(response.content):
            entity.source_id = self.source_id
            yield entity

    @classmethod
    def parse_announcements(
        cls,
        payload: Any,
        *,
        entity: EntityRef | None = None,
    ) -> Iterable[FilingRef]:
        if isinstance(payload, dict):
            rows = payload.get("Table") or payload.get("table") or payload.get("data") or []
        else:
            rows = payload if isinstance(payload, list) else []
        if isinstance(rows, str):
            import json

            rows = json.loads(rows)
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("SCRIP_CD") or row.get("SCRIPCODE") or row.get("scripCode") or "").strip()
            name = str(row.get("SLONGNAME") or row.get("SCRIP_NAME") or row.get("companyName") or "").strip()
            title = str(row.get("HEADLINE") or row.get("NEWSSUB") or row.get("subject") or "").strip()
            filed_at, published_at = _parse_date(
                row.get("NEWS_DT") or row.get("DT_TM") or row.get("DissemDT") or row.get("date")
            )
            attachment_name = str(
                row.get("ATTACHMENTNAME") or row.get("ATTACHMENT") or row.get("attachment") or ""
            ).strip()
            attachment = None
            if attachment_name:
                attachment = (
                    attachment_name
                    if attachment_name.startswith("http")
                    else cls.ATTACHMENT_BASE + attachment_name.lstrip("/")
                )
            news_id = str(row.get("NEWSID") or row.get("ID") or row.get("newsId") or "").strip()
            if not news_id:
                news_id = hashlib.sha256(
                    f"{code}|{published_at}|{title}|{attachment}".encode("utf-8")
                ).hexdigest()[:24]
            source_entity_id = entity.source_entity_id if entity else code
            entity_id = entity.entity_id if entity else f"bse-{code or news_id}"
            detail_url = row.get("NSURL") or row.get("URL") or row.get("detailUrl")
            yield FilingRef(
                source_id="bse_india",
                filing_id=news_id,
                entity_id=entity_id,
                source_entity_id=source_entity_id or None,
                form=_filing_form(title),
                title=title or news_id,
                filed_at=filed_at,
                detail_url=str(detail_url) if detail_url else None,
                primary_document_url=attachment,
                language="en",
                amendment=any(token in title.lower() for token in ("revised", "corrigendum", "clarification")),
                metadata={
                    **row,
                    "security_code": code,
                    "issuer_name": name,
                    "published_at": published_at.isoformat() if published_at else None,
                },
            )

    def list_recent_filings(
        self,
        *,
        begin: date,
        end: date,
        entity: EntityRef | None = None,
        page_size: int = 100,
        max_pages: int | None = None,
        category: str = "-1",
        **_: Any,
    ) -> Iterable[FilingRef]:
        page = 1
        while max_pages is None or page <= max_pages:
            params = {
                "Pageno": page,
                "strCat": category,
                "strPrevDate": begin.strftime("%Y%m%d"),
                "strScrip": entity.source_entity_id if entity else "",
                "strSearch": "P",
                "strToDate": end.strftime("%Y%m%d"),
                "strType": "C",
                "PageSize": min(max(page_size, 1), 100),
            }
            payload = self.client.get_json(
                self.FILING_URL,
                params=params,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": self.REFERER,
                    "Origin": "https://www.bseindia.com",
                },
            )
            rows = payload.get("Table") if isinstance(payload, dict) else payload
            rows = rows or []
            for filing in self.parse_announcements(payload, entity=entity):
                filing.source_id = self.source_id
                yield filing
            if not rows or len(rows) < page_size:
                break
            page += 1

    def list_filings(
        self,
        entity: EntityRef,
        *,
        begin: date,
        end: date,
        **kwargs: Any,
    ) -> Iterable[FilingRef]:
        yield from self.list_recent_filings(begin=begin, end=end, entity=entity, **kwargs)

    def list_documents(self, filing: FilingRef, **_: Any) -> Iterable[DocumentRef]:
        if filing.primary_document_url:
            filename = filing.primary_document_url.split("?", 1)[0].rsplit("/", 1)[-1]
            yield DocumentRef(
                source_id=self.source_id,
                document_id=f"{filing.filing_id}:attachment",
                filing_id=filing.filing_id,
                entity_id=filing.entity_id,
                url=filing.primary_document_url,
                filename=filename or f"{filing.filing_id}.bin",
                document_type=filing.form,
                filed_at=filing.filed_at,
            )

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        if offline:
            content = (
                b"Security Code,Issuer Name,Security Id,Status,ISIN No\n"
                b"500325,Reliance Industries Ltd,RELIANCE,Active,INE002A01018\n"
            )
            entities = list(self.parse_universe(content))
            filings = list(
                self.parse_announcements(
                    {
                        "Table": [
                            {
                                "SCRIP_CD": "500325",
                                "SLONGNAME": "Reliance Industries Ltd",
                                "HEADLINE": "Financial Results",
                                "NEWS_DT": "10/07/2026 17:31:00",
                                "NEWSID": "20260710001",
                                "ATTACHMENTNAME": "RIL_Results.pdf",
                            }
                        ]
                    }
                )
            )
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="universe_and_corporate_announcements",
                endpoint=self.UNIVERSE_URL,
                records_observed=len(entities) + len(filings),
                message="BSE company-master and corporate-announcement contracts validated offline.",
            )
        try:
            response = self.client.request("GET", self.UNIVERSE_URL)
            count = sum(1 for _ in self.parse_universe(response.content))
            if count < 3_000:
                raise ValueError(
                    f"BSE response produced only {count} rows; expected at least 3000"
                )
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="company_master",
                endpoint=self.UNIVERSE_URL,
                records_observed=count,
                message="BSE company list returned parseable rows.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("company_master", self.UNIVERSE_URL, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="company_master",
                endpoint=self.UNIVERSE_URL,
                message=f"BSE smoke check failed: {type(exc).__name__}: {exc}",
            )
