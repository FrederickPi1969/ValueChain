from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any
from urllib.parse import urljoin

from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import DocumentRef, EntityRef, FilingRef, SmokeResult, SmokeStatus


class FilingsXbrlAdapter(BaseAdapter):
    BASE_URL = "https://filings.xbrl.org"
    API_BASE = "https://filings.xbrl.org/api"

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = f"{self.API_BASE}/filings"
        if offline:
            sample = {
                "data": [
                    {
                        "type": "filing",
                        "id": "lei/example/2025-12-31/ESEF/GB/0",
                        "attributes": {
                            "country": "GB",
                            "date_added": "2026-01-02",
                            "report_date": "2025-12-31",
                            "viewer_url": "https://example.invalid/viewer",
                        },
                    }
                ]
            }
            count = len(list(self.parse_filings(sample)))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="global_ixbrl_index",
                endpoint=endpoint,
                records_observed=count,
                message="Offline filings.xbrl.org JSON:API filing contract validated.",
            )
        try:
            payload = self.client.get_json(endpoint, params={"page[size]": 1, "sort": "-processed"})
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="global_ixbrl_index",
                endpoint=endpoint,
                records_observed=len(payload.get("data", [])),
                message="filings.xbrl.org public JSON:API returned filing records.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("global_ixbrl_index", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="global_ixbrl_index",
                endpoint=endpoint,
                message=f"filings.xbrl.org smoke check failed: {type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _date(value: str | None) -> date | None:
        return date.fromisoformat(value[:10]) if value else None

    @classmethod
    def parse_filings(
        cls, payload: dict[str, Any], entity_id: str | None = None
    ) -> Iterable[FilingRef]:
        included_entities = {
            str(item.get("id")): item.get("attributes", {})
            for item in payload.get("included", [])
            if item.get("type") == "entity"
        }
        for item in payload.get("data", []):
            attrs = item.get("attributes", {})
            api_filing_id = str(item.get("id"))
            relation = item.get("relationships", {}).get("entity", {}).get("data", {})
            included = included_entities.get(str(relation.get("id")), {})
            lei = (
                attrs.get("lei")
                or attrs.get("entity_identifier")
                or included.get("identifier")
            )
            filing_id = str(attrs.get("fxo_id") or api_filing_id)
            canonical_entity = entity_id or (
                f"lei-{lei}" if lei else f"xbrl-entity-{filing_id}"
            )
            viewer_url = attrs.get("viewer_url") or attrs.get("filing_url")
            yield FilingRef(
                source_id="filings_xbrl",
                filing_id=filing_id,
                entity_id=canonical_entity,
                source_entity_id=lei,
                form=attrs.get("filing_system") or "iXBRL",
                title=attrs.get("name") or attrs.get("document_type") or filing_id,
                filed_at=cls._date(attrs.get("date_added") or attrs.get("processed")),
                period_end=cls._date(attrs.get("period_end") or attrs.get("report_date")),
                detail_url=urljoin(cls.BASE_URL, viewer_url) if viewer_url else None,
                primary_document_url=(
                    urljoin(cls.BASE_URL, attrs["package_url"])
                    if attrs.get("package_url")
                    else urljoin(cls.BASE_URL, attrs["report_url"])
                    if attrs.get("report_url")
                    else None
                ),
                language=attrs.get("language"),
                amendment=False,
                metadata={
                    **attrs,
                    "api_filing_id": api_filing_id,
                    "entity_identifier": lei,
                    "entity_name": included.get("name"),
                    "package_url": (
                        urljoin(cls.BASE_URL, attrs["package_url"])
                        if attrs.get("package_url")
                        else None
                    ),
                    "report_url": (
                        urljoin(cls.BASE_URL, attrs["report_url"])
                        if attrs.get("report_url")
                        else None
                    ),
                    "json_url": (
                        urljoin(cls.BASE_URL, attrs["json_url"])
                        if attrs.get("json_url")
                        else None
                    ),
                },
            )

    def list_filings(
        self,
        entity: EntityRef | None = None,
        *,
        jurisdiction: str | None = None,
        page_size: int = 100,
        max_pages: int | None = 1,
        sort: str = "-processed",
        **_: Any,
    ) -> Iterable[FilingRef]:
        page = 1
        while max_pages is None or page <= max_pages:
            params: dict[str, Any] = {
                "page[size]": min(page_size, 200),
                "page[number]": page,
                "sort": sort,
                "include": "entity",
            }
            if jurisdiction:
                params["filter[country]"] = jurisdiction.upper()
            if entity and entity.lei:
                params["filter[entity.identifier]"] = entity.lei
            payload = self.client.get_json(f"{self.API_BASE}/filings", params=params)
            rows = payload.get("data", [])
            for filing in self.parse_filings(payload, entity.entity_id if entity else None):
                filing.source_id = self.source_id
                yield filing
            if not rows or not payload.get("links", {}).get("next"):
                break
            page += 1

    def list_documents(self, filing: FilingRef, **_: Any) -> Iterable[DocumentRef]:
        candidates = [
            ("package", filing.metadata.get("package_url"), "original XBRL Report Package"),
            ("report", filing.metadata.get("report_url"), "primary Inline XBRL report"),
            ("xbrl-json", filing.metadata.get("json_url"), "xBRL-JSON facts"),
        ]
        for suffix, url, kind in candidates:
            if not url:
                continue
            filename = str(url).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or f"{suffix}.bin"
            yield DocumentRef(
                source_id=self.source_id,
                document_id=f"{filing.filing_id}:{suffix}",
                filing_id=filing.filing_id,
                entity_id=filing.entity_id,
                url=str(url),
                filename=filename,
                document_type=kind,
                filed_at=filing.filed_at,
            )
