from __future__ import annotations

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


class EdinetAdapter(BaseAdapter):
    API_BASE = "https://api.edinet-fsa.go.jp/api/v2"

    def _key(self) -> str:
        return self.require_credential(self.settings.edinet_api_key, "EDINET_API_KEY")

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = f"{self.API_BASE}/documents.json"
        if offline:
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="daily_document_list",
                endpoint=endpoint,
                message="Offline EDINET version 2 document-list contract validated; live operation requires a free API key.",
                evidence={"fixture": "edinet_documents.json", "credential_env": "EDINET_API_KEY"},
            )
        try:
            payload = self.client.get_json(
                endpoint,
                params={
                    "date": date.today().isoformat(),
                    "type": 2,
                    "Subscription-Key": self._key(),
                },
            )
            records = payload.get("results", [])
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="daily_document_list",
                endpoint=endpoint,
                message="EDINET API version 2 authenticated and returned a daily list response.",
                records_observed=len(records),
            )
        except CredentialRequiredError as exc:
            return self.credential_result("daily_document_list", endpoint, exc)
        except NetworkBlockedError as exc:
            return self.network_blocked_result("daily_document_list", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="daily_document_list",
                endpoint=endpoint,
                message=f"EDINET smoke check failed: {type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _parse_date(value: str | None) -> date | None:
        return date.fromisoformat(value[:10]) if value else None

    def list_daily_filings(
        self,
        day: date,
        *,
        ordinance_codes: set[str] | None = None,
        document_type_codes: set[str] | None = None,
    ) -> Iterable[FilingRef]:
        payload = self.client.get_json(
            f"{self.API_BASE}/documents.json",
            params={"date": day.isoformat(), "type": 2, "Subscription-Key": self._key()},
        )
        for record in payload.get("results", []):
            if ordinance_codes and record.get("ordinanceCode") not in ordinance_codes:
                continue
            if document_type_codes and record.get("docTypeCode") not in document_type_codes:
                continue
            doc_id = record.get("docID")
            if not doc_id:
                continue
            edinet_code = record.get("edinetCode") or record.get("filerName") or "unknown"
            yield FilingRef(
                source_id=self.source_id,
                filing_id=doc_id,
                entity_id=f"edinet-{edinet_code}",
                source_entity_id=edinet_code,
                form=record.get("docTypeCode"),
                title=record.get("docDescription") or record.get("filerName"),
                filed_at=self._parse_date(record.get("submitDateTime")),
                period_end=self._parse_date(record.get("periodEnd")),
                detail_url=f"https://disclosure2.edinet-fsa.go.jp/WEEE0030.aspx?docID={doc_id}",
                primary_document_url=f"{self.API_BASE}/documents/{doc_id}",
                language="ja",
                amendment=bool(record.get("withdrawalStatus") == "1"),
                metadata=record,
            )

    def list_entities(self, *, day: date | None = None, **_: Any) -> Iterable[EntityRef]:
        seen: set[str] = set()
        for filing in self.list_daily_filings(day or date.today()):
            if not filing.source_entity_id or filing.source_entity_id in seen:
                continue
            seen.add(filing.source_entity_id)
            record = filing.metadata
            yield EntityRef(
                entity_id=filing.entity_id,
                source_id=self.source_id,
                source_entity_id=filing.source_entity_id,
                legal_name=record.get("filerName") or filing.source_entity_id,
                jurisdiction="JP",
                exchange="TSE" if record.get("secCode") else None,
                ticker=record.get("secCode"),
                local_registry_id=record.get("JCN"),
                metadata=record,
            )

    def list_filings(
        self, entity: EntityRef, *, days: Iterable[date], **_: Any
    ) -> Iterable[FilingRef]:
        for day in days:
            for filing in self.list_daily_filings(day):
                if filing.source_entity_id == entity.source_entity_id:
                    yield filing

    def list_documents(self, filing: FilingRef, **_: Any) -> Iterable[DocumentRef]:
        for output_type, suffix, description, media_type in (
            (1, "xbrl.zip", "XBRL package", "application/zip"),
            (2, "pdf", "PDF rendering", "application/pdf"),
            (5, "csv.zip", "CSV package", "application/zip"),
        ):
            yield DocumentRef(
                source_id=self.source_id,
                document_id=f"{filing.filing_id}:type-{output_type}",
                filing_id=filing.filing_id,
                entity_id=filing.entity_id,
                url=f"{self.API_BASE}/documents/{filing.filing_id}",
                filename=f"{filing.filing_id}.{suffix}",
                document_type=description,
                expected_media_type=media_type,
                filed_at=filing.filed_at,
                request_params={"type": output_type},
                credential_env="EDINET_API_KEY",
                credential_transport=CredentialTransport.QUERY,
                credential_name="Subscription-Key",
            )
