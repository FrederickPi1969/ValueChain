from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import requests

from valuechain.acquisition_state import AcquisitionIssuer, AcquisitionState, iso_now
from valuechain.proxy_pool import ProxyEndpoint, ProxyPoolClient
from valuechain.rate_limit import RateLimiter


SEC_DATA_BASE = "https://data.sec.gov"
SEC_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
TIER_A_FORMS = {
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "8-K",
    "8-K/A",
    "20-F",
    "20-F/A",
    "6-K",
    "6-K/A",
    "40-F",
    "40-F/A",
}
US_PRIMARY_EXCHANGES = {"NYSE", "NASDAQ", "CBOE"}


@dataclass(frozen=True)
class AcquisitionConfig:
    raw_root: Path
    state_path: Path
    proxy_pool_url: str
    sec_user_agent: str
    start_date: str = "2026-01-01"
    requests_per_second: float = 1.0
    request_timeout_seconds: int = 60
    request_retries: int = 3
    issuer_limit: int = 3
    rescan_hours: int = 24

    @classmethod
    def from_env(cls) -> AcquisitionConfig:
        return cls(
            raw_root=Path(
                os.getenv("VALUECHAIN_FILING_RAW_DIR", "/mnt/hdd8tb/filings")
            ).expanduser(),
            state_path=Path(
                os.getenv(
                    "VALUECHAIN_ACQUISITION_STATE",
                    "/home/pi/valuechain-state/acquisition.sqlite3",
                )
            ).expanduser(),
            proxy_pool_url=os.getenv(
                "VALUECHAIN_PROXY_POOL_URL", "https://proxy.frederickpi.com"
            ),
            sec_user_agent=os.getenv(
                "VALUECHAIN_SEC_USER_AGENT",
                "FrederickPi ValueChain/0.1 contact=frederickpi1969@gmail.com",
            ),
            start_date=os.getenv("VALUECHAIN_ACQUISITION_START_DATE", "2026-01-01"),
            requests_per_second=float(os.getenv("VALUECHAIN_ACQUISITION_SEC_RPS", "1.0")),
            request_timeout_seconds=int(
                os.getenv("VALUECHAIN_ACQUISITION_TIMEOUT_SECONDS", "60")
            ),
            request_retries=int(os.getenv("VALUECHAIN_ACQUISITION_RETRIES", "3")),
            issuer_limit=int(os.getenv("VALUECHAIN_ACQUISITION_ISSUER_LIMIT", "3")),
            rescan_hours=int(os.getenv("VALUECHAIN_ACQUISITION_RESCAN_HOURS", "24")),
        )


class SecProxySession:
    def __init__(self, config: AcquisitionConfig, proxy_pool: ProxyPoolClient) -> None:
        self.config = config
        self.proxy_pool = proxy_pool
        self.rate_limiter = RateLimiter(config.requests_per_second)
        self.session = requests.Session()
        self.proxy: ProxyEndpoint | None = None

    def rotate_proxy(self) -> ProxyEndpoint:
        self.proxy = self.proxy_pool.random_normal()
        return self.proxy

    def get(self, url: str, accept: str, stream: bool = False) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.config.request_retries + 1):
            if self.proxy is None:
                self.rotate_proxy()
            proxy_url = self.proxy.url
            self.rate_limiter.wait()
            try:
                response = self.session.get(
                    url,
                    headers={
                        "User-Agent": self.config.sec_user_agent,
                        "Accept": accept,
                        "Accept-Encoding": "gzip, deflate",
                    },
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=self.config.request_timeout_seconds,
                    stream=stream,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    response.close()
                    raise requests.HTTPError(f"retryable status {response.status_code}")
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                self.rotate_proxy()
                time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"SEC request failed after proxy retries: {type(last_error).__name__}")

    def get_json(self, url: str) -> dict[str, Any]:
        response = self.get(url, accept="application/json")
        try:
            payload = response.json()
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise ValueError(f"Expected JSON object from {url}")
        return payload


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(partial, path)


def download_atomic(
    session: SecProxySession,
    url: str,
    path: Path,
    accept: str = "*/*",
) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return {
            "source_url": url,
            "local_path": str(path),
            "content_type": "",
            "byte_size": path.stat().st_size,
            "sha256": hash_file(path),
            "retrieved_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            "status": "complete",
            "cached": True,
        }
    partial = path.with_name(f"{path.name}.partial")
    response = session.get(url, accept=accept, stream=True)
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                byte_size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        content_type = response.headers.get("content-type", "")
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        response.close()
    os.replace(partial, path)
    return {
        "source_url": url,
        "local_path": str(path),
        "content_type": content_type,
        "byte_size": byte_size,
        "sha256": digest.hexdigest(),
        "retrieved_at": iso_now(),
        "status": "complete",
        "cached": False,
    }


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_company_universe(payload: dict, priority_tickers: dict[str, int]) -> list[AcquisitionIssuer]:
    fields = payload.get("fields", [])
    issuers: dict[str, AcquisitionIssuer] = {}
    for values in payload.get("data", []):
        row = dict(zip(fields, values, strict=False))
        cik = str(row.get("cik", "")).zfill(10)
        ticker = str(row.get("ticker", "")).upper()
        exchange = str(row.get("exchange", "")).upper()
        if not cik.strip("0") or not ticker:
            continue
        if ticker in priority_tickers:
            priority = priority_tickers[ticker]
        elif exchange in US_PRIMARY_EXCHANGES:
            priority = 100
        else:
            priority = 500
        candidate = AcquisitionIssuer(
            cik=cik,
            ticker=ticker,
            company_name=str(row.get("name", "")),
            exchange=exchange,
            priority=priority,
        )
        existing = issuers.get(cik)
        if existing is None or candidate.priority < existing.priority:
            issuers[cik] = candidate
    return sorted(issuers.values(), key=lambda row: (row.priority, row.ticker, row.cik))


def load_priority_tickers(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    priorities: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            ticker = str(row.get("ticker", "")).upper()
            if ticker:
                priorities[ticker] = index
    return priorities


def parse_submission_rows(payload: dict, cik: str, start_date: str) -> list[dict[str, str]]:
    recent = payload.get("filings", {}).get("recent", {})
    return parse_submission_columns(recent, cik=cik, start_date=start_date)


def parse_submission_columns(columns: dict, cik: str, start_date: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    accessions = columns.get("accessionNumber", [])
    for index, accession in enumerate(accessions):
        filing_date = column_value(columns, "filingDate", index)
        form = column_value(columns, "form", index)
        if not accession or filing_date < start_date or form not in TIER_A_FORMS:
            continue
        accession_no_dashes = accession.replace("-", "")
        cik_numeric = str(int(cik))
        archive_url = f"{SEC_ARCHIVE_BASE}/{cik_numeric}/{accession_no_dashes}/"
        rows.append(
            {
                "cik": cik.zfill(10),
                "accession_number": accession,
                "accession_no_dashes": accession_no_dashes,
                "form": form,
                "filing_date": filing_date,
                "report_date": column_value(columns, "reportDate", index),
                "accepted_at": column_value(columns, "acceptanceDateTime", index),
                "primary_document": column_value(columns, "primaryDocument", index),
                "archive_url": archive_url,
            }
        )
    return rows


def column_value(columns: dict, name: str, index: int) -> str:
    values = columns.get(name, [])
    if index >= len(values) or values[index] is None:
        return ""
    return str(values[index])


class SecAcquisitionRunner:
    def __init__(self, config: AcquisitionConfig, repository_root: Path) -> None:
        self.config = config
        self.repository_root = repository_root
        self.proxy_pool = ProxyPoolClient(config.proxy_pool_url)
        self.config.raw_root.mkdir(parents=True, exist_ok=True)

    def refresh_universe(self, state: AcquisitionState) -> int:
        session = SecProxySession(self.config, self.proxy_pool)
        payload = session.get_json("https://www.sec.gov/files/company_tickers_exchange.json")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        catalog_path = (
            self.config.raw_root
            / "sec_edgar"
            / "_catalog"
            / f"company_tickers_exchange.{timestamp}.json"
        )
        atomic_write_json(catalog_path, payload)
        priority_tickers = load_priority_tickers(
            self.repository_root / "data" / "universe" / "ai_infra_universe.csv"
        )
        issuers = parse_company_universe(payload, priority_tickers)
        return state.upsert_issuers(issuers)

    def run_batch(self) -> dict[str, object]:
        run_id = datetime.now(UTC).strftime("sec-2026-%Y%m%dT%H%M%SZ")
        counts = {"issuers": 0, "filings": 0, "documents": 0, "errors": 0}
        with AcquisitionState(self.config.state_path) as state:
            stats = state.stats()
            if not stats["issuers"] or self.universe_refresh_due():
                self.proxy_pool.health()
                self.refresh_universe(state)
            state.begin_run(run_id)
            issuers = state.claim_issuers(
                self.config.issuer_limit,
                rescan_hours=self.config.rescan_hours,
            )
            for issuer in issuers:
                counts["issuers"] += 1
                try:
                    result = self.acquire_issuer(state, issuer)
                    counts["filings"] += result["filings"]
                    counts["documents"] += result["documents"]
                    state.complete_issuer(issuer.cik)
                except Exception as exc:
                    counts["errors"] += 1
                    state.fail_issuer(issuer.cik, f"{type(exc).__name__}: {exc}")
            status = "complete" if counts["errors"] == 0 else "partial"
            state.finish_run(run_id, status, counts)
            return {"run_id": run_id, "status": status, "counts": counts, "state": state.stats()}

    def universe_refresh_due(self, max_age_hours: int = 24) -> bool:
        catalog_dir = self.config.raw_root / "sec_edgar" / "_catalog"
        snapshots = list(catalog_dir.glob("company_tickers_exchange.*.json"))
        if not snapshots:
            return True
        newest_mtime = max(path.stat().st_mtime for path in snapshots)
        return time.time() - newest_mtime >= max_age_hours * 3600

    def acquire_issuer(self, state: AcquisitionState, issuer: AcquisitionIssuer) -> dict[str, int]:
        session = SecProxySession(self.config, self.proxy_pool)
        session.rotate_proxy()
        payload = session.get_json(f"{SEC_DATA_BASE}/submissions/CIK{issuer.cik}.json")
        filings = parse_submission_rows(payload, cik=issuer.cik, start_date=self.config.start_date)
        for history in payload.get("filings", {}).get("files", []):
            filing_to = str(history.get("filingTo", ""))
            if filing_to and filing_to < self.config.start_date:
                continue
            name = str(history.get("name", ""))
            if not name:
                continue
            historical = session.get_json(f"{SEC_DATA_BASE}/submissions/{name}")
            filings.extend(
                parse_submission_columns(historical, cik=issuer.cik, start_date=self.config.start_date)
            )
        unique_filings = {row["accession_number"]: row for row in filings}
        document_count = 0
        for filing in sorted(unique_filings.values(), key=lambda row: row["filing_date"]):
            document_count += self.acquire_filing(state, session, filing)
        return {"filings": len(unique_filings), "documents": document_count}

    def acquire_filing(
        self,
        state: AcquisitionState,
        session: SecProxySession,
        filing: dict[str, str],
    ) -> int:
        local_dir = (
            self.config.raw_root
            / "sec_edgar"
            / filing["filing_date"][:4]
            / filing["filing_date"][5:7]
            / filing["cik"][:4]
            / filing["cik"]
            / filing["accession_no_dashes"]
        )
        state.upsert_filing(filing, local_dir, status="downloading")
        documents = [
            (
                "archive_index",
                f"{filing['archive_url']}index.json",
                local_dir / "archive_index.json",
                "application/json",
            ),
            (
                "complete_submission",
                f"{filing['archive_url']}{filing['accession_number']}.txt",
                local_dir / "complete_submission.txt",
                "text/plain,*/*",
            ),
        ]
        if filing.get("primary_document"):
            documents.append(
                (
                    "primary_document",
                    f"{filing['archive_url']}{filing['primary_document']}",
                    local_dir / filing["primary_document"],
                    "text/html,application/xhtml+xml,text/plain,*/*",
                )
            )
        manifest_documents: list[dict[str, object]] = []
        try:
            for kind, url, path, accept in documents:
                result = download_atomic(session, url, path, accept=accept)
                result.update(
                    {
                        "accession_number": filing["accession_number"],
                        "document_kind": kind,
                    }
                )
                state.upsert_document(result)
                manifest_documents.append(result)
            atomic_write_json(
                local_dir / "filing.json",
                {
                    "source_id": "sec_edgar",
                    "retrieved_at": iso_now(),
                    "filing": filing,
                    "documents": manifest_documents,
                },
            )
            state.upsert_filing(filing, local_dir, status="complete")
        except Exception as exc:
            state.upsert_filing(
                filing,
                local_dir,
                status="retry",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        return len(manifest_documents)
