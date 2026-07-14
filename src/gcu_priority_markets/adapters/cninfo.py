from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import DocumentRef, EntityRef, FilingRef, SmokeResult, SmokeStatus


class CninfoAdapter(BaseAdapter):
    """CNINFO issuer denominator and statutory-announcement discovery."""

    # Despite its historical filename, CNINFO's official stock map contains the
    # combined Shanghai, Shenzhen and Beijing A-share issuer mapping.  A single
    # fetch also prevents denominator drift caused by separately timed files.
    UNIVERSE_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
    MARKET_URLS = {"SSE": UNIVERSE_URL, "SZSE": UNIVERSE_URL, "BSE": UNIVERSE_URL}
    # CNINFO serves Beijing issuer-filtered results through the combined
    # `szse` search column; the intuitive `bse` value returns an empty set.
    MARKET_COLUMNS = {"SSE": "sse", "SZSE": "szse", "BSE": "szse"}
    MARKET_MICS = {"SSE": "XSHG", "SZSE": "XSHE", "BSE": "XBSE"}
    MIC_MARKETS = {value: key for key, value in MARKET_MICS.items()}
    FINANCIAL_REPORT_CATEGORIES = ";".join(
        (
            "category_ndbg_szsh",
            "category_bndbg_szsh",
            "category_yjdbg_szsh",
            "category_sjdbg_szsh",
        )
    )
    FILING_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    DOCUMENT_BASE = "https://static.cninfo.com.cn/"
    MIN_UNIVERSE_ROWS = 5_000
    TARGET_UNIVERSE_ROWS = 6_000
    MAX_UNIVERSE_ATTEMPTS = 3
    LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")

    @staticmethod
    def _stock_rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("stockList", "stock_list", "data", "records", "rows"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []

    @staticmethod
    def _infer_market(row: dict[str, Any], code: str) -> str | None:
        explicit = str(
            row.get("market")
            or row.get("exchange")
            or row.get("column")
            or row.get("plate")
            or ""
        ).strip().lower()
        token_map = {
            "sse": "SSE",
            "sh": "SSE",
            "shmb": "SSE",
            "shkcp": "SSE",
            "szse": "SZSE",
            "sz": "SZSE",
            "szmb": "SZSE",
            "szcy": "SZSE",
            "bse": "BSE",
            "bj": "BSE",
        }
        if explicit in token_map:
            return token_map[explicit]
        org_id = str(row.get("orgId") or row.get("org_id") or "").lower()
        if org_id.startswith("gssh"):
            return "SSE"
        if org_id.startswith("gssz"):
            return "SZSE"
        normalized = code.zfill(6)
        if normalized.startswith(("4", "8", "92")):
            return "BSE"
        if normalized.startswith(("6", "9")):
            return "SSE"
        if normalized.startswith(("0", "1", "2", "3")):
            return "SZSE"
        return None

    @classmethod
    def parse_universe(
        cls,
        payload: Any,
        market: str | None = None,
    ) -> Iterable[EntityRef]:
        requested_market = market.upper() if market else None
        for row in cls._stock_rows(payload):
            code = str(
                row.get("code")
                or row.get("secCode")
                or row.get("stockCode")
                or row.get("scode")
                or ""
            ).strip()
            name = str(
                row.get("zwjc")
                or row.get("secName")
                or row.get("name")
                or row.get("stockName")
                or ""
            ).strip()
            org_id = str(row.get("orgId") or row.get("org_id") or "").strip()
            if not code or not name:
                continue
            exchange = cls._infer_market(row, code) or requested_market
            if requested_market and exchange != requested_market:
                continue
            if not exchange:
                continue
            source_entity_id = org_id or f"{exchange}:{code}"
            aliases = [
                str(value).strip()
                for value in (row.get("pinyin"), row.get("ywqc"), row.get("fullName"))
                if value and str(value).strip() != name
            ]
            yield EntityRef(
                entity_id=f"cninfo-{source_entity_id}",
                source_id="cninfo",
                source_entity_id=source_entity_id,
                legal_name=name,
                jurisdiction="CN",
                exchange=cls.MARKET_MICS[exchange],
                ticker=code.zfill(6),
                local_registry_id=org_id or None,
                aliases=aliases,
                metadata={**row, "market": exchange},
            )

    def _fetch_validated_universe(self) -> list[EntityRef]:
        headers = {
            "Referer": "https://www.cninfo.com.cn/new/index",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        best: list[EntityRef] = []
        for attempt in range(self.MAX_UNIVERSE_ATTEMPTS):
            payload = self.client.get_json(self.UNIVERSE_URL, headers=headers)
            rows = list(self.parse_universe(payload))
            if len(rows) > len(best):
                best = rows
            if len(rows) >= self.TARGET_UNIVERSE_ROWS:
                return rows
            if attempt + 1 < self.MAX_UNIVERSE_ATTEMPTS:
                self.client.rotate_proxy()
        if len(best) >= self.MIN_UNIVERSE_ROWS:
            return best
        raise ValueError(
            f"CNINFO response produced only {len(best)} issuer rows after "
            f"{self.MAX_UNIVERSE_ATTEMPTS} attempts; expected at least "
            f"{self.MIN_UNIVERSE_ROWS}"
        )

    def list_entities(self, *, markets: Iterable[str] | None = None, **_: Any) -> Iterable[EntityRef]:
        selected = {market.upper() for market in (markets or self.MARKET_URLS)}
        unknown = selected.difference(self.MARKET_URLS)
        if unknown:
            raise ValueError(f"Unsupported CNINFO markets: {sorted(unknown)}")
        seen: set[str] = set()
        for entity in self._fetch_validated_universe():
            source_market = str(entity.metadata.get("market") or "")
            if source_market not in selected or entity.source_entity_id in seen:
                continue
            seen.add(entity.source_entity_id)
            entity.source_id = self.source_id
            yield entity

    @staticmethod
    def _date_from_millis(value: Any) -> tuple[date | None, datetime | None]:
        if value in (None, ""):
            return None, None
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            moment = datetime.fromtimestamp(timestamp, tz=UTC)
            return moment.astimezone(CninfoAdapter.LOCAL_TIMEZONE).date(), moment
        except (TypeError, ValueError, OSError):
            text = str(value).strip().replace("/", "-")
            try:
                parsed = datetime.fromisoformat(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=CninfoAdapter.LOCAL_TIMEZONE)
                return (
                    parsed.astimezone(CninfoAdapter.LOCAL_TIMEZONE).date(),
                    parsed.astimezone(UTC),
                )
            except ValueError:
                try:
                    parsed_date = date.fromisoformat(text[:10])
                    local_moment = datetime.combine(
                        parsed_date,
                        datetime.min.time(),
                        tzinfo=CninfoAdapter.LOCAL_TIMEZONE,
                    )
                    return parsed_date, local_moment.astimezone(UTC)
                except ValueError:
                    return None, None

    @staticmethod
    def _classify(title: str) -> str:
        normalized = title.replace(" ", "").lower()
        mapping = (
            ("半年度报告", "semiannual_report"),
            ("半年报", "semiannual_report"),
            ("中期报告", "semiannual_report"),
            ("semi-annualreport", "semiannual_report"),
            ("semiannualreport", "semiannual_report"),
            ("interimreport", "semiannual_report"),
            ("第一季度", "q1_report"),
            ("一季度", "q1_report"),
            ("一季报", "q1_report"),
            ("firstquarter", "q1_report"),
            ("reportofq1", "q1_report"),
            ("q1report", "q1_report"),
            ("第三季度", "q3_report"),
            ("三季度", "q3_report"),
            ("三季报", "q3_report"),
            ("thirdquarter", "q3_report"),
            ("reportofq3", "q3_report"),
            ("q3report", "q3_report"),
            ("年度报告", "annual_report"),
            ("年报", "annual_report"),
            ("annualreport", "annual_report"),
            ("季度报告", "quarterly_report"),
            ("业绩快报", "earnings_flash"),
            ("业绩预告", "earnings_forecast"),
        )
        for marker, form in mapping:
            if marker in normalized:
                return form
        return "announcement"

    @classmethod
    def parse_announcements(
        cls,
        payload: dict[str, Any],
        *,
        entity: EntityRef | None = None,
        market: str | None = None,
    ) -> Iterable[FilingRef]:
        rows = payload.get("announcements") or payload.get("records") or payload.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("records") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            announcement_id = str(
                row.get("announcementId") or row.get("announcement_id") or row.get("id") or ""
            ).strip()
            code = str(row.get("secCode") or row.get("stockCode") or row.get("code") or "").strip()
            name = str(row.get("secName") or row.get("stockName") or row.get("name") or "").strip()
            title = str(row.get("announcementTitle") or row.get("title") or "").strip()
            adjunct = str(row.get("adjunctUrl") or row.get("downloadUrl") or "").strip()
            if not announcement_id:
                announcement_id = f"{code}:{row.get('announcementTime')}:{title}"
            filed_at, accepted_at = cls._date_from_millis(
                row.get("announcementTime") or row.get("announcementDate") or row.get("publishTime")
            )
            primary_url = None
            if adjunct:
                primary_url = adjunct if adjunct.startswith("http") else cls.DOCUMENT_BASE + adjunct.lstrip("/")
            source_entity_id = (
                entity.source_entity_id
                if entity
                else str(row.get("orgId") or row.get("org_id") or code).strip()
            )
            entity_id = entity.entity_id if entity else f"cninfo-{source_entity_id}"
            yield FilingRef(
                source_id="cninfo",
                filing_id=announcement_id,
                entity_id=entity_id,
                source_entity_id=source_entity_id,
                form=cls._classify(title),
                title=title or announcement_id,
                filed_at=filed_at,
                detail_url=f"https://www.cninfo.com.cn/new/disclosure/detail?announcementId={announcement_id}",
                primary_document_url=primary_url,
                language="zh",
                amendment=any(marker in title for marker in ("更正", "修订", "补充", "取消")),
                metadata={
                    **row,
                    "market": market or (entity.exchange if entity else None),
                    "security_code": code,
                    "issuer_name": name,
                    "published_at": accepted_at.isoformat() if accepted_at else None,
                },
            )

    def list_recent_filings(
        self,
        *,
        begin: date,
        end: date,
        markets: Iterable[str] | None = None,
        entity: EntityRef | None = None,
        page_size: int = 30,
        max_pages: int | None = None,
        category: str = "",
    ) -> Iterable[FilingRef]:
        selected = [market.upper() for market in (markets or self.MARKET_COLUMNS)]
        if entity and entity.exchange:
            exchange = entity.exchange.upper()
            selected = [
                str(entity.metadata.get("market") or self.MIC_MARKETS.get(exchange) or exchange)
                .upper()
            ]
        headers = {
            "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
            "Origin": "https://www.cninfo.com.cn",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        for market in selected:
            page = 1
            while max_pages is None or page <= max_pages:
                stock = ""
                if entity:
                    stock = f"{entity.ticker or ''},{entity.source_entity_id or ''}".strip(",")
                data = {
                    "pageNum": str(page),
                    "pageSize": str(min(max(page_size, 1), 100)),
                    "column": self.MARKET_COLUMNS[market],
                    "tabName": "fulltext",
                    "plate": "",
                    "stock": stock,
                    "searchkey": "",
                    "secid": "",
                    "category": category,
                    "trade": "",
                    "seDate": f"{begin.isoformat()}~{end.isoformat()}",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "true",
                }
                response = self.client.request("POST", self.FILING_URL, data=data, headers=headers)
                payload = response.json()
                rows = payload.get("announcements") or []
                for filing in self.parse_announcements(payload, entity=entity, market=market):
                    filing.source_id = self.source_id
                    yield filing
                total_pages = int(payload.get("totalpages") or payload.get("totalPages") or page)
                has_more = payload.get("hasMore")
                if not rows or page >= total_pages or has_more is False:
                    break
                page += 1

    def list_filings(
        self,
        entity: EntityRef,
        *,
        begin: date,
        end: date,
        page_size: int = 30,
        max_pages: int | None = None,
        category: str = "",
        **_: Any,
    ) -> Iterable[FilingRef]:
        yield from self.list_recent_filings(
            begin=begin,
            end=end,
            entity=entity,
            page_size=page_size,
            max_pages=max_pages,
            category=category,
        )

    def list_documents(self, filing: FilingRef, **_: Any) -> Iterable[DocumentRef]:
        if filing.primary_document_url:
            suffix = filing.primary_document_url.split("?", 1)[0].rsplit("/", 1)[-1]
            filename = suffix if suffix.lower().endswith(".pdf") else f"{filing.filing_id}.pdf"
            yield DocumentRef(
                source_id=self.source_id,
                document_id=f"{filing.filing_id}:primary",
                filing_id=filing.filing_id,
                entity_id=filing.entity_id,
                url=filing.primary_document_url,
                filename=filename,
                document_type=filing.form or "announcement",
                expected_media_type="application/pdf",
                filed_at=filing.filed_at,
            )

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = self.UNIVERSE_URL
        if offline:
            payload = {"stockList": [{"code": "000001", "zwjc": "平安银行", "orgId": "gssz0000001"}]}
            count = len(list(self.parse_universe(payload, "SZSE")))
            filing_payload = {
                "announcements": [
                    {
                        "announcementId": "1210000001",
                        "secCode": "000001",
                        "secName": "平安银行",
                        "announcementTitle": "2025年年度报告",
                        "announcementTime": 1770000000000,
                        "adjunctUrl": "finalpage/2026-03-01/1210000001.PDF",
                    }
                ]
            }
            filing_count = len(list(self.parse_announcements(filing_payload, market="SZSE")))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="issuer_universe_and_announcement_search",
                endpoint=endpoint,
                records_observed=count + filing_count,
                message="CNINFO issuer JSON and announcement-search contracts validated offline.",
            )
        try:
            count = len(self._fetch_validated_universe())
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="issuer_universe",
                endpoint=endpoint,
                records_observed=count,
                message="CNINFO issuer reference endpoint returned parseable records.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("issuer_universe", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="issuer_universe",
                endpoint=endpoint,
                message=f"CNINFO smoke check failed: {type(exc).__name__}: {exc}",
            )
