from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import requests

from valuechain.models import Company, FilingRecord
from valuechain.rate_limit import RateLimiter


SEC_DATA_BASE = "https://data.sec.gov"
SEC_WWW_BASE = "https://www.sec.gov"


class SECClient:
    def __init__(
        self,
        user_agent: str,
        requests_per_second: float = 2.0,
        proxies: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> None:
        self.timeout = timeout
        self.rate_limiter = RateLimiter(requests_per_second)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )
        if proxies:
            self.session.proxies.update({key.rstrip(":/"): value for key, value in proxies.items()})

    def get_json(self, url: str) -> Any:
        self.rate_limiter.wait()
        headers = self._headers_for(url, accept="application/json")
        response = self.session.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def get_bytes(self, url: str) -> bytes:
        self.rate_limiter.wait()
        headers = self._headers_for(url, accept="text/html,application/xhtml+xml,text/plain,*/*")
        response = self.session.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return response.content

    def _headers_for(self, url: str, accept: str) -> dict[str, str]:
        host = "data.sec.gov" if url.startswith(SEC_DATA_BASE) else "www.sec.gov"
        return {"Host": host, "Accept": accept}

    def fetch_company_tickers_exchange(self) -> dict[str, dict[str, str]]:
        payload = self.get_json(f"{SEC_WWW_BASE}/files/company_tickers_exchange.json")
        rows = payload.get("data", [])
        fields = payload.get("fields", [])
        lookup: dict[str, dict[str, str]] = {}
        for values in rows:
            row = dict(zip(fields, values, strict=False))
            ticker = str(row.get("ticker", "")).upper()
            if ticker:
                lookup[ticker] = {
                    "ticker": ticker,
                    "cik": str(row.get("cik", "")).zfill(10),
                    "company_name": str(row.get("name", "")),
                    "exchange": str(row.get("exchange", "")),
                }
        return lookup

    def resolve_companies(self, companies: list[Company]) -> list[Company]:
        lookup = self.fetch_company_tickers_exchange()
        resolved: list[Company] = []
        for company in companies:
            match = lookup.get(company.ticker.upper())
            if match:
                company.cik = match["cik"]
                company.exchange = match["exchange"]
                if not company.company_name:
                    company.company_name = match["company_name"]
            resolved.append(company)
        return resolved

    def submissions(self, cik: str) -> dict[str, Any]:
        return self.get_json(f"{SEC_DATA_BASE}/submissions/CIK{cik.zfill(10)}.json")

    def discover_filings(
        self,
        company: Company,
        forms: set[str],
        max_filings: int,
    ) -> list[FilingRecord]:
        if not company.cik:
            return []
        payload = self.submissions(company.cik)
        recent = payload.get("filings", {}).get("recent", {})
        accessions = recent.get("accessionNumber", [])
        discovered: list[FilingRecord] = []
        for idx, accession in enumerate(accessions):
            form = _get_recent_value(recent, "form", idx)
            if form not in forms:
                continue
            cik_no_leading = str(int(company.cik))
            accession_no_dashes = accession.replace("-", "")
            primary_doc = _get_recent_value(recent, "primaryDocument", idx)
            archive_url = f"{SEC_WWW_BASE}/Archives/edgar/data/{cik_no_leading}/{accession_no_dashes}/"
            primary_url = f"{archive_url}{primary_doc}" if primary_doc else archive_url
            discovered.append(
                FilingRecord(
                    ticker=company.ticker,
                    cik=company.cik,
                    company_name=company.company_name,
                    form=form,
                    accession_number=accession,
                    filing_date=_get_recent_value(recent, "filingDate", idx),
                    report_date=_get_recent_value(recent, "reportDate", idx),
                    accepted_timestamp=_get_recent_value(recent, "acceptanceDateTime", idx),
                    primary_document=primary_doc,
                    archive_url=archive_url,
                    primary_document_url=primary_url,
                )
            )
            if len(discovered) >= max_filings:
                break
        return discovered

    def download_primary_document(self, filing: FilingRecord, raw_dir: Path) -> FilingRecord:
        if not filing.primary_document_url:
            return filing
        target_dir = raw_dir / filing.ticker / filing.accession_no_dashes()
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filing.primary_document
        if target_path.exists() and target_path.stat().st_size > 0:
            data = target_path.read_bytes()
        else:
            data = self.get_bytes(filing.primary_document_url)
            target_path.write_bytes(data)
        filing.local_path = str(target_path)
        filing.sha256 = hashlib.sha256(data).hexdigest()
        metadata_path = target_dir / "metadata.json"
        metadata_path.write_text(json.dumps(filing.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return filing


def _get_recent_value(recent: dict[str, list[Any]], key: str, idx: int) -> str:
    values = recent.get(key, [])
    if idx >= len(values):
        return ""
    value = values[idx]
    return "" if value is None else str(value)
