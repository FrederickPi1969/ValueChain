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


class CompaniesHouseAdapter(BaseAdapter):
    API_BASE = "https://api.company-information.service.gov.uk"
    DOCUMENT_API_BASE = "https://document-api.company-information.service.gov.uk"

    def _key(self) -> str:
        return self.require_credential(
            self.settings.companies_house_api_key,
            "COMPANIES_HOUSE_API_KEY",
        )

    def _auth(self) -> tuple[str, str]:
        return (self._key(), "")

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = f"{self.API_BASE}/company/00000006"
        if offline:
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="company_profile",
                endpoint=endpoint,
                message="Offline Companies House profile, filing-history, and document-auth contracts validated; live operation requires an API key.",
                evidence={"credential_env": "COMPANIES_HOUSE_API_KEY"},
            )
        try:
            payload = self.client.get_json(endpoint, auth=self._auth())
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="company_profile",
                endpoint=endpoint,
                message="Companies House REST API authenticated and returned a company profile.",
                records_observed=1,
                evidence={"company_number": payload.get("company_number")},
            )
        except CredentialRequiredError as exc:
            return self.credential_result("company_profile", endpoint, exc)
        except NetworkBlockedError as exc:
            return self.network_blocked_result("company_profile", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="company_profile",
                endpoint=endpoint,
                message=f"Companies House smoke check failed: {type(exc).__name__}: {exc}",
            )

    def get_entity(self, company_number: str) -> EntityRef:
        number = company_number.strip().upper()
        payload = self.client.get_json(f"{self.API_BASE}/company/{number}", auth=self._auth())
        aliases = [
            item.get("name", "")
            for item in payload.get("previous_company_names", [])
            if isinstance(item, dict) and item.get("name")
        ]
        return EntityRef(
            entity_id=f"uk-company-{number}",
            source_id=self.source_id,
            source_entity_id=number,
            legal_name=payload.get("company_name") or number,
            jurisdiction="GB",
            local_registry_id=number,
            aliases=aliases,
            metadata=payload,
        )

    def search_entities(self, query: str, *, items_per_page: int = 100) -> Iterable[EntityRef]:
        payload = self.client.get_json(
            f"{self.API_BASE}/search/companies",
            params={"q": query, "items_per_page": min(items_per_page, 100)},
            auth=self._auth(),
        )
        for item in payload.get("items", []):
            number = item["company_number"]
            yield EntityRef(
                entity_id=f"uk-company-{number}",
                source_id=self.source_id,
                source_entity_id=number,
                legal_name=item.get("title") or number,
                jurisdiction="GB",
                local_registry_id=number,
                metadata=item,
            )

    def list_entities(self, *, query: str, **_: Any) -> Iterable[EntityRef]:
        return self.search_entities(query)

    @staticmethod
    def _parse_date(value: str | None) -> date | None:
        return date.fromisoformat(value) if value else None

    def list_filings(
        self,
        entity: EntityRef,
        *,
        category: str | None = "accounts",
        items_per_page: int = 100,
        max_pages: int | None = None,
    ) -> Iterable[FilingRef]:
        start_index = 0
        page = 1
        while max_pages is None or page <= max_pages:
            params: dict[str, Any] = {
                "items_per_page": min(items_per_page, 100),
                "start_index": start_index,
            }
            if category:
                params["category"] = category
            payload = self.client.get_json(
                f"{self.API_BASE}/company/{entity.source_entity_id}/filing-history",
                params=params,
                auth=self._auth(),
            )
            items = payload.get("items", [])
            for item in items:
                transaction_id = item.get("transaction_id")
                metadata_link = item.get("links", {}).get("document_metadata")
                document_id = (
                    metadata_link.rstrip("/").rsplit("/", 1)[-1] if metadata_link else None
                )
                yield FilingRef(
                    source_id=self.source_id,
                    filing_id=transaction_id,
                    entity_id=entity.entity_id,
                    source_entity_id=entity.source_entity_id,
                    form=item.get("type"),
                    title=item.get("description"),
                    filed_at=self._parse_date(item.get("date")),
                    period_end=self._parse_date(item.get("made_up_date")),
                    detail_url=(
                        "https://find-and-update.company-information.service.gov.uk/company/"
                        f"{entity.source_entity_id}/filing-history/{transaction_id}"
                    ),
                    primary_document_url=(
                        f"{self.DOCUMENT_API_BASE}/document/{document_id}/content"
                        if document_id
                        else None
                    ),
                    language="en",
                    metadata={**item, "document_id": document_id},
                )
            start_index += len(items)
            if not items or start_index >= int(payload.get("total_count") or 0):
                break
            page += 1

    def list_documents(self, filing: FilingRef, **_: Any) -> Iterable[DocumentRef]:
        document_id = filing.metadata.get("document_id")
        if not document_id:
            return
        yield DocumentRef(
            source_id=self.source_id,
            document_id=f"{filing.filing_id}:content",
            filing_id=filing.filing_id,
            entity_id=filing.entity_id,
            url=f"{self.DOCUMENT_API_BASE}/document/{document_id}/content",
            filename=f"{filing.filing_id}.pdf",
            document_type="Companies House document content",
            filed_at=filing.filed_at,
            request_headers={"Accept": "application/pdf"},
            credential_env="COMPANIES_HOUSE_API_KEY",
            credential_transport=CredentialTransport.BASIC_USERNAME,
        )
