from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import EntityRef, SmokeResult, SmokeStatus


class GleifAdapter(BaseAdapter):
    API_BASE = "https://api.gleif.org/api/v1"

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = f"{self.API_BASE}/lei-records"
        if offline:
            sample = {
                "data": [
                    {
                        "id": "5493001KJTIIGC8Y1R12",
                        "attributes": {
                            "entity": {
                                "legalName": {"name": "Example Entity"},
                                "legalAddress": {"country": "US"},
                            }
                        },
                    }
                ]
            }
            count = len(list(self.parse_entities(sample)))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="lei_entity_search",
                endpoint=endpoint,
                records_observed=count,
                message="Offline GLEIF JSON:API entity contract validated.",
            )
        try:
            payload = self.client.get_json(endpoint, params={"page[size]": 1})
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="lei_entity_search",
                endpoint=endpoint,
                records_observed=len(payload.get("data", [])),
                message="GLEIF public API returned LEI records.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("lei_entity_search", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="lei_entity_search",
                endpoint=endpoint,
                message=f"GLEIF smoke check failed: {type(exc).__name__}: {exc}",
            )

    @classmethod
    def parse_entities(cls, payload: dict[str, Any]) -> Iterable[EntityRef]:
        for item in payload.get("data", []):
            lei = item.get("id")
            attributes = item.get("attributes", {})
            entity = attributes.get("entity", {})
            legal_name = entity.get("legalName", {}).get("name") or lei
            address = entity.get("legalAddress", {})
            aliases = [
                alias.get("name", "")
                for alias in entity.get("otherNames", [])
                if isinstance(alias, dict) and alias.get("name")
            ]
            yield EntityRef(
                entity_id=f"lei-{lei}",
                source_id="gleif",
                source_entity_id=lei,
                legal_name=legal_name,
                jurisdiction=address.get("country"),
                lei=lei,
                local_registry_id=(entity.get("registeredAs") or None),
                aliases=aliases,
                metadata=attributes,
            )

    def list_entities(
        self,
        *,
        query: str | None = None,
        jurisdiction: str | None = None,
        page_size: int = 100,
        max_pages: int | None = 1,
        **_: Any,
    ) -> Iterable[EntityRef]:
        page = 1
        while max_pages is None or page <= max_pages:
            params: dict[str, Any] = {"page[size]": min(page_size, 200), "page[number]": page}
            if query:
                params["filter[entity.legalName]"] = query
            if jurisdiction:
                params["filter[entity.legalAddress.country]"] = jurisdiction.upper()
            payload = self.client.get_json(f"{self.API_BASE}/lei-records", params=params)
            rows = payload.get("data", [])
            for entity in self.parse_entities(payload):
                entity.source_id = self.source_id
                yield entity
            if not rows or not payload.get("links", {}).get("next"):
                break
            page += 1
