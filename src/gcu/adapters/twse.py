from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import EntityRef, SmokeResult, SmokeStatus


class TwseOpenApiAdapter(BaseAdapter):
    COMPANY_ENDPOINT = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = self.COMPANY_ENDPOINT
        if offline:
            sample = [
                {"公司代號": "2330", "公司名稱": "台積電", "公司簡稱": "台積電", "產業別": "24"}
            ]
            count = len(list(self.parse_companies(sample)))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="listed_company_universe",
                endpoint=endpoint,
                records_observed=count,
                message="Offline TWSE OpenAPI listed-company contract validated.",
            )
        try:
            payload = self.client.get_json(endpoint)
            count = sum(1 for _ in self.parse_companies(payload))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="listed_company_universe",
                endpoint=endpoint,
                records_observed=count,
                message="TWSE OpenAPI returned the listed-company universe.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("listed_company_universe", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="listed_company_universe",
                endpoint=endpoint,
                message=f"TWSE smoke check failed: {type(exc).__name__}: {exc}",
            )

    @classmethod
    def parse_companies(cls, payload: list[dict[str, Any]]) -> Iterable[EntityRef]:
        for row in payload:
            ticker = str(row.get("公司代號") or row.get("CompanyCode") or "").strip()
            if not ticker:
                continue
            legal_name = str(
                row.get("公司名稱") or row.get("CompanyName") or row.get("公司簡稱") or ticker
            ).strip()
            short_name = str(row.get("公司簡稱") or "").strip()
            yield EntityRef(
                entity_id=f"twse-{ticker}",
                source_id="twse",
                source_entity_id=ticker,
                legal_name=legal_name,
                jurisdiction="TW",
                exchange="TWSE",
                ticker=ticker,
                local_registry_id=str(row.get("統一編號") or "").strip() or None,
                aliases=[short_name] if short_name and short_name != legal_name else [],
                metadata=row,
            )

    def list_entities(self, **_: Any) -> Iterable[EntityRef]:
        payload = self.client.get_json(self.COMPANY_ENDPOINT)
        for entity in self.parse_companies(payload):
            entity.source_id = self.source_id
            yield entity


class TpexOpenApiAdapter(BaseAdapter):
    """Taipei Exchange listed-company universe from the official OpenAPI."""

    COMPANY_ENDPOINT = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = self.COMPANY_ENDPOINT
        if offline:
            sample = [
                {"公司代號": "1240", "公司名稱": "茂生農經股份有限公司", "公司簡稱": "茂生農經"}
            ]
            count = len(list(self.parse_companies(sample)))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="listed_company_universe",
                endpoint=endpoint,
                records_observed=count,
                message="Offline TPEx OpenAPI listed-company contract validated.",
            )
        try:
            payload = self.client.get_json(endpoint)
            count = sum(1 for _ in self.parse_companies(payload))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="listed_company_universe",
                endpoint=endpoint,
                records_observed=count,
                message="TPEx OpenAPI returned the listed-company universe.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("listed_company_universe", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="listed_company_universe",
                endpoint=endpoint,
                message=f"TPEx smoke check failed: {type(exc).__name__}: {exc}",
            )

    @classmethod
    def parse_companies(cls, payload: list[dict[str, Any]]) -> Iterable[EntityRef]:
        for row in payload:
            ticker = str(row.get("公司代號") or row.get("CompanyCode") or "").strip()
            if not ticker:
                continue
            legal_name = str(
                row.get("公司名稱") or row.get("CompanyName") or row.get("公司簡稱") or ticker
            ).strip()
            short_name = str(row.get("公司簡稱") or "").strip()
            yield EntityRef(
                entity_id=f"tpex-{ticker}",
                source_id="tpex",
                source_entity_id=ticker,
                legal_name=legal_name,
                jurisdiction="TW",
                exchange="TPEx",
                ticker=ticker,
                local_registry_id=str(
                    row.get("營利事業統一編號") or row.get("統一編號") or ""
                ).strip()
                or None,
                aliases=[short_name] if short_name and short_name != legal_name else [],
                metadata=row,
            )

    def list_entities(self, **_: Any) -> Iterable[EntityRef]:
        payload = self.client.get_json(self.COMPANY_ENDPOINT)
        for entity in self.parse_companies(payload):
            entity.source_id = self.source_id
            yield entity
