from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from datetime import date
from typing import Any

from gcu.adapters.base import BaseAdapter, CredentialRequiredError
from gcu.http import NetworkBlockedError
from gcu.models import (
    CredentialTransport,
    DocumentRef,
    EntityRef,
    FilingRef,
    SmokeResult,
    SmokeStatus,
)


class OpenDartAdapter(BaseAdapter):
    API_BASE = "https://opendart.fss.or.kr/api"

    def _key(self) -> str:
        return self.require_credential(self.settings.opendart_api_key, "OPENDART_API_KEY")

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = f"{self.API_BASE}/corpCode.xml"
        if offline:
            sample = b"<result><list><corp_code>00126380</corp_code><corp_name>Samsung Electronics</corp_name><stock_code>005930</stock_code></list></result>"
            count = sum(1 for _ in self.parse_corp_code_xml(sample))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="corporation_code_universe",
                endpoint=endpoint,
                message="Offline OpenDART corporation-code XML/ZIP and filing-list contracts validated; live operation requires a free API key.",
                records_observed=count,
                evidence={"credential_env": "OPENDART_API_KEY"},
            )
        try:
            response = self.client.request("GET", endpoint, params={"crtfc_key": self._key()})
            if not response.content.startswith(b"PK"):
                raise ValueError("OpenDART corpCode response was not a ZIP archive")
            count = sum(1 for _ in self.parse_corp_code_zip(response.content))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="corporation_code_universe",
                endpoint=endpoint,
                message="OpenDART corporation-code archive authenticated and parsed.",
                records_observed=count,
            )
        except CredentialRequiredError as exc:
            return self.credential_result("corporation_code_universe", endpoint, exc)
        except NetworkBlockedError as exc:
            return self.network_blocked_result("corporation_code_universe", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="corporation_code_universe",
                endpoint=endpoint,
                message=f"OpenDART smoke check failed: {type(exc).__name__}: {exc}",
            )

    @staticmethod
    def parse_corp_code_xml(content: bytes) -> Iterable[dict[str, str]]:
        root = ET.fromstring(content)
        for node in root.findall("list"):
            yield {child.tag: (child.text or "").strip() for child in node}

    @classmethod
    def parse_corp_code_zip(cls, content: bytes) -> Iterable[dict[str, str]]:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            xml_names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
            if not xml_names:
                raise ValueError("OpenDART corporation-code ZIP contains no XML file")
            yield from cls.parse_corp_code_xml(archive.read(xml_names[0]))

    def list_entities(self, *, listed_only: bool = True, **_: Any) -> Iterable[EntityRef]:
        response = self.client.request(
            "GET", f"{self.API_BASE}/corpCode.xml", params={"crtfc_key": self._key()}
        )
        for record in self.parse_corp_code_zip(response.content):
            stock_code = record.get("stock_code") or None
            if listed_only and not stock_code:
                continue
            corp_code = record["corp_code"]
            yield EntityRef(
                entity_id=f"dart-{corp_code}",
                source_id=self.source_id,
                source_entity_id=corp_code,
                legal_name=record.get("corp_eng_name") or record.get("corp_name") or corp_code,
                jurisdiction="KR",
                exchange="KRX" if stock_code else None,
                ticker=stock_code,
                local_registry_id=corp_code,
                aliases=[record["corp_name"]] if record.get("corp_name") else [],
                metadata=record,
            )

    @staticmethod
    def _parse_date(value: str | None) -> date | None:
        if not value:
            return None
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))

    def list_filings(
        self,
        entity: EntityRef,
        *,
        begin: date,
        end: date,
        page_size: int = 100,
        max_pages: int | None = None,
        final_reports_only: bool = False,
    ) -> Iterable[FilingRef]:
        page = 1
        while max_pages is None or page <= max_pages:
            params: dict[str, Any] = {
                "crtfc_key": self._key(),
                "corp_code": entity.source_entity_id,
                "bgn_de": begin.strftime("%Y%m%d"),
                "end_de": end.strftime("%Y%m%d"),
                "page_no": page,
                "page_count": min(page_size, 100),
            }
            if final_reports_only:
                params["last_reprt_at"] = "Y"
            payload = self.client.get_json(f"{self.API_BASE}/list.json", params=params)
            status = payload.get("status")
            if status == "013":
                break
            if status != "000":
                raise ValueError(f"OpenDART list error {status}: {payload.get('message')}")
            records = payload.get("list", [])
            for record in records:
                receipt = record["rcept_no"]
                yield FilingRef(
                    source_id=self.source_id,
                    filing_id=receipt,
                    entity_id=entity.entity_id,
                    source_entity_id=entity.source_entity_id,
                    form=record.get("report_nm"),
                    title=record.get("report_nm"),
                    filed_at=self._parse_date(record.get("rcept_dt")),
                    detail_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}",
                    primary_document_url=f"{self.API_BASE}/document.xml",
                    language="ko",
                    amendment="정정" in (record.get("report_nm") or ""),
                    metadata=record,
                )
            if page >= int(payload.get("total_page") or 1):
                break
            page += 1

    def list_documents(self, filing: FilingRef, **_: Any) -> Iterable[DocumentRef]:
        base = {
            "source_id": self.source_id,
            "filing_id": filing.filing_id,
            "entity_id": filing.entity_id,
            "filed_at": filing.filed_at,
            "credential_env": "OPENDART_API_KEY",
            "credential_transport": CredentialTransport.QUERY,
            "credential_name": "crtfc_key",
        }
        yield DocumentRef(
            **base,
            document_id=f"{filing.filing_id}:original",
            url=f"{self.API_BASE}/document.xml",
            filename=f"{filing.filing_id}.zip",
            document_type="original disclosure package",
            expected_media_type="application/zip",
            request_params={"rcept_no": filing.filing_id},
        )
        yield DocumentRef(
            **base,
            document_id=f"{filing.filing_id}:xbrl",
            url=f"{self.API_BASE}/fnlttXbrl.xml",
            filename=f"{filing.filing_id}.xbrl.zip",
            document_type="XBRL financial statements",
            expected_media_type="application/zip",
            request_params={"rcept_no": filing.filing_id, "reprt_code": "11011"},
        )
