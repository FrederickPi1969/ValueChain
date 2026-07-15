from __future__ import annotations

import asyncio
import csv
import json
import os
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from pathlib import Path
from typing import Any

import httpx


PILOT_EXTRA_COMPANIES: tuple[dict[str, Any], ...] = (
    {"ticker": "AAPL", "company_name": "Apple Inc.", "role": "consumer_technology", "priority": 1},
    {"ticker": "JPM", "company_name": "JPMorgan Chase & Co.", "role": "financial_infrastructure", "priority": 2},
    {"ticker": "GS", "company_name": "Goldman Sachs Group Inc.", "role": "financial_infrastructure", "priority": 3},
    {"ticker": "WMT", "company_name": "Walmart Inc.", "role": "retail_logistics", "priority": 2},
    {"ticker": "COST", "company_name": "Costco Wholesale Corporation", "role": "retail_logistics", "priority": 2},
    {"ticker": "HD", "company_name": "Home Depot Inc.", "role": "retail_distribution", "priority": 3},
    {"ticker": "COP", "company_name": "ConocoPhillips", "role": "energy", "priority": 1},
    {"ticker": "CVX", "company_name": "Chevron Corporation", "role": "energy", "priority": 1},
    {"ticker": "CAT", "company_name": "Caterpillar Inc.", "role": "industrial_equipment", "priority": 2},
    {"ticker": "HON", "company_name": "Honeywell International Inc.", "role": "industrial_automation", "priority": 2},
    {"ticker": "BA", "company_name": "Boeing Company", "role": "aerospace", "priority": 1},
    {"ticker": "LMT", "company_name": "Lockheed Martin Corporation", "role": "defense", "priority": 1},
    {"ticker": "RTX", "company_name": "RTX Corporation", "role": "aerospace_defense", "priority": 1},
    {"ticker": "NOC", "company_name": "Northrop Grumman Corporation", "role": "defense", "priority": 1},
    {"ticker": "UNH", "company_name": "UnitedHealth Group Incorporated", "role": "healthcare_services", "priority": 2},
    {"ticker": "JNJ", "company_name": "Johnson & Johnson", "role": "healthcare", "priority": 2},
    {"ticker": "PFE", "company_name": "Pfizer Inc.", "role": "biopharma", "priority": 2},
    {"ticker": "LLY", "company_name": "Eli Lilly and Company", "role": "biopharma", "priority": 1},
    {"ticker": "KO", "company_name": "Coca-Cola Company", "role": "consumer_staples", "priority": 3},
    {"ticker": "PEP", "company_name": "PepsiCo Inc.", "role": "consumer_staples", "priority": 3},
    {"ticker": "UPS", "company_name": "United Parcel Service Inc.", "role": "transportation_logistics", "priority": 2},
)


@dataclass(frozen=True, slots=True)
class CatalogConfig:
    base_url: str = "http://127.0.0.1:18018"
    token: str = dataclass_field(default_factory=lambda: os.getenv("VALUECHAIN_FILE_API_TOKEN", ""))
    timeout_s: float = 30.0
    concurrency: int = 8
    filing_root: Path = Path("/mnt/hdd8tb/filings/sec_edgar")


class AcquisitionCatalogClient:
    def __init__(self, config: CatalogConfig) -> None:
        self.config = config
        self._semaphore = asyncio.Semaphore(max(1, config.concurrency))
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=config.timeout_s,
        )

    async def __aenter__(self) -> AcquisitionCatalogClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def latest_annual_filing(self, company: dict[str, Any]) -> dict[str, Any]:
        ticker = str(company["ticker"]).upper()
        issuer = await self._resolve_issuer(ticker, str(company.get("company_name") or ""))
        if issuer is None:
            return {**company, "status": "missing_catalog_issuer", "error": "No exact ticker in issuer registry"}
        issuer_id = str(issuer.get("source_issuer_id") or "")
        rows: list[dict[str, Any]] = []
        for form in ("10-K", "20-F", "40-F"):
            rows.extend(await self._query_filings(issuer_id, form))
        selected = max(rows, key=filing_sort_key) if rows else None
        if selected is None:
            return {**company, "status": "missing_catalog_filing", "error": "No complete annual filing"}
        manifest_path = filing_manifest_path(selected, self.config.filing_root)
        local_document = resolve_primary_document(manifest_path)
        return {
            **company,
            "status": "ready" if local_document else "missing_local_document",
            "source_id": "sec_edgar",
            "cik": str(selected.get("source_issuer_id") or "").zfill(10),
            "accession_number": str(selected.get("source_filing_id") or ""),
            "form": str(selected.get("form_raw") or ""),
            "filing_date": str(selected.get("filing_date") or ""),
            "report_date": str(selected.get("report_date") or ""),
            "accepted_timestamp": str(selected.get("accepted_at") or ""),
            "archive_url": str(selected.get("archive_url") or ""),
            "filing_manifest_path": str(manifest_path),
            "local_path": str(local_document or ""),
            "source_document_url": primary_document_url(manifest_path),
            "error": "" if local_document else "Primary document is not present on the filing HDD",
        }

    async def _resolve_issuer(self, ticker: str, company_name: str) -> dict[str, Any] | None:
        for query in [ticker, *issuer_search_terms(company_name)]:
            async with self._semaphore:
                response = await self._client.get(
                    "/api/acquisition/issuers",
                    params={"source_id": "sec_edgar", "q": query, "limit": 100},
                )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("items", []) if isinstance(payload, dict) else []
            selected = select_exact_issuer(rows, ticker)
            if selected is not None:
                return selected
        return None

    async def _query_filings(self, issuer_id: str, form: str) -> list[dict[str, Any]]:
        async with self._semaphore:
            response = await self._client.get(
                "/api/acquisition/filings",
                params={
                    "source_id": "sec_edgar",
                    "form": form,
                    "status": "complete",
                    "issuer_id": issuer_id,
                    "limit": 10,
                },
            )
        response.raise_for_status()
        payload = response.json()
        return payload.get("items", []) if isinstance(payload, dict) else []


def load_pilot_universe(path: Path, *, target_count: int = 100) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    combined = [*rows, *PILOT_EXTRA_COMPANIES]
    unique: dict[str, dict[str, Any]] = {}
    for row in combined:
        ticker = str(row.get("ticker") or "").upper().strip()
        if not ticker or ticker in unique:
            continue
        unique[ticker] = {
            "ticker": ticker,
            "company_name": str(row.get("company_name") or "").strip(),
            "universe_role": str(row.get("role") or "").strip(),
            "priority": int(row.get("priority") or 3),
            "universe_notes": str(row.get("notes") or "").strip(),
        }
    return list(unique.values())[:target_count]


async def build_filing_manifest(
    companies: list[dict[str, Any]],
    config: CatalogConfig,
) -> list[dict[str, Any]]:
    async with AcquisitionCatalogClient(config) as client:
        return await asyncio.gather(*(client.latest_annual_filing(company) for company in companies))


def filing_manifest_path(row: dict[str, Any], filing_root: Path) -> Path:
    filing_date = date.fromisoformat(str(row["filing_date"])[:10])
    cik = str(row["source_issuer_id"]).zfill(10)
    accession = str(row["source_filing_id"]).replace("-", "")
    return filing_root / f"{filing_date.year:04d}" / f"{filing_date.month:02d}" / cik[:4] / cik / accession / "filing.json"


def resolve_primary_document(manifest_path: Path) -> Path | None:
    if not manifest_path.exists():
        return None
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for document in payload.get("documents", []):
        if document.get("document_kind") != "primary_document" or document.get("status") != "complete":
            continue
        local_path = Path(str(document.get("local_path") or ""))
        if local_path.is_file():
            return local_path
    primary_name = str(payload.get("filing", {}).get("primary_document") or "")
    fallback = manifest_path.parent / primary_name
    return fallback if primary_name and fallback.is_file() else None


def primary_document_url(manifest_path: Path) -> str:
    if not manifest_path.exists():
        return ""
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for document in payload.get("documents", []):
        if document.get("document_kind") == "primary_document":
            return str(document.get("source_url") or document.get("final_url") or "")
    return ""


def filing_sort_key(row: dict[str, Any]) -> tuple[str, int]:
    form_priority = {"10-K": 3, "20-F": 2, "40-F": 1}
    return str(row.get("filing_date") or ""), form_priority.get(str(row.get("form_raw") or ""), 0)


def select_exact_issuer(rows: list[dict[str, Any]], ticker: str) -> dict[str, Any] | None:
    normalized = ticker.upper().strip()
    exact = [row for row in rows if str(row.get("ticker") or "").upper().strip() == normalized]
    return max(exact, key=lambda row: int(row.get("filing_count") or 0)) if exact else None


def issuer_search_terms(company_name: str) -> list[str]:
    ignored = {"company", "corporation", "corp", "inc", "incorporated", "limited", "ltd", "plc", "holdings"}
    tokens = [
        token
        for token in re.findall(r"[A-Za-z0-9]+", company_name)
        if token.lower() not in ignored and len(token) >= 3
    ]
    return sorted(dict.fromkeys(tokens), key=lambda token: (-len(token), token.lower()))[:3]
