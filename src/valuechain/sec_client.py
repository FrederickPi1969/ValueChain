from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
import requests

from valuechain.models import Company, FilingRecord, SourceDocument
from valuechain.rate_limit import RateLimiter


SEC_DATA_BASE = "https://data.sec.gov"
SEC_WWW_BASE = "https://www.sec.gov"
FORM_PRIORITY = ("10-K", "20-F", "10-Q", "8-K", "6-K")
DEFAULT_EXHIBIT_TYPES = ("EX-10", "EX-21", "EX-99", "EX-99.1")
TEXT_DOCUMENT_EXTENSIONS = {".htm", ".html", ".txt"}
SKIPPED_ARCHIVE_TYPES = {
    "GRAPHIC",
    "XML",
    "EXCEL",
    "ZIP",
    "JSON",
}


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

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8", errors="replace")

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
        filing_date_from: str = "",
        filing_date_to: str = "",
        selection: str = "form_balanced",
    ) -> list[FilingRecord]:
        if not company.cik:
            return []
        payload = self.submissions(company.cik)
        recent = payload.get("filings", {}).get("recent", {})
        accessions = recent.get("accessionNumber", [])
        candidates: list[FilingRecord] = []
        for idx, accession in enumerate(accessions):
            form = _get_recent_value(recent, "form", idx)
            if form not in forms:
                continue
            filing_date = _get_recent_value(recent, "filingDate", idx)
            if filing_date_from and filing_date < filing_date_from:
                continue
            if filing_date_to and filing_date > filing_date_to:
                continue
            cik_no_leading = str(int(company.cik))
            accession_no_dashes = accession.replace("-", "")
            primary_doc = _get_recent_value(recent, "primaryDocument", idx)
            archive_url = f"{SEC_WWW_BASE}/Archives/edgar/data/{cik_no_leading}/{accession_no_dashes}/"
            primary_url = f"{archive_url}{primary_doc}" if primary_doc else archive_url
            candidates.append(
                FilingRecord(
                    ticker=company.ticker,
                    cik=company.cik,
                    company_name=company.company_name,
                    form=form,
                    accession_number=accession,
                    filing_date=filing_date,
                    report_date=_get_recent_value(recent, "reportDate", idx),
                    accepted_timestamp=_get_recent_value(recent, "acceptanceDateTime", idx),
                    primary_document=primary_doc,
                    archive_url=archive_url,
                    primary_document_url=primary_url,
                )
            )
            if selection == "latest" and len(candidates) >= max_filings:
                break
        if selection == "latest":
            return candidates
        return select_form_balanced_filings(candidates, forms=forms, max_per_form=max_filings)

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

    def fetch_filing_detail_documents(self, filing: FilingRecord, raw_dir: Path) -> list[dict[str, str]]:
        target_dir = raw_dir / filing.ticker / filing.accession_no_dashes()
        target_dir.mkdir(parents=True, exist_ok=True)
        detail_path = target_dir / "filing_detail.html"
        detail_url = f"{filing.archive_url}{filing.accession_number}-index.html"
        if detail_path.exists() and detail_path.stat().st_size > 0:
            html = detail_path.read_text(encoding="utf-8", errors="replace")
        else:
            html = self.get_text(detail_url)
            detail_path.write_text(html, encoding="utf-8")
        rows = parse_filing_detail_rows(html)
        if rows:
            return rows
        return self.fetch_archive_index_documents(filing, raw_dir)

    def fetch_archive_index_documents(self, filing: FilingRecord, raw_dir: Path) -> list[dict[str, str]]:
        target_dir = raw_dir / filing.ticker / filing.accession_no_dashes()
        target_dir.mkdir(parents=True, exist_ok=True)
        index_path = target_dir / "archive_index.json"
        index_url = f"{filing.archive_url}index.json"
        if index_path.exists() and index_path.stat().st_size > 0:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        else:
            payload = self.get_json(index_url)
            index_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        items = payload.get("directory", {}).get("item", [])
        rows: list[dict[str, str]] = []
        for idx, item in enumerate(items, start=1):
            document = str(item.get("name", ""))
            if not document:
                continue
            document_type = filing.form if document == filing.primary_document else infer_document_type(document)
            rows.append(
                {
                    "sequence": str(idx),
                    "description": "primary document" if document == filing.primary_document else "",
                    "document": document,
                    "document_type": document_type,
                    "size": str(item.get("size", "")),
                }
            )
        return rows

    def discover_source_documents(
        self,
        filing: FilingRecord,
        raw_dir: Path,
        include_exhibits: bool = True,
        exhibit_types: tuple[str, ...] = DEFAULT_EXHIBIT_TYPES,
        max_exhibits_per_filing: int = 8,
    ) -> list[SourceDocument]:
        rows = self.fetch_filing_detail_documents(filing, raw_dir)
        documents = build_source_documents(
            filing,
            rows,
            include_exhibits=include_exhibits,
            exhibit_types=exhibit_types,
            max_exhibits_per_filing=max_exhibits_per_filing,
        )
        if not any(document.is_primary for document in documents) and filing.primary_document:
            documents.insert(0, source_document_from_row(filing, primary_document_row(filing), is_primary=True))
        return documents

    def download_source_documents(
        self,
        filing: FilingRecord,
        raw_dir: Path,
        include_exhibits: bool = True,
        exhibit_types: tuple[str, ...] = DEFAULT_EXHIBIT_TYPES,
        max_exhibits_per_filing: int = 8,
    ) -> list[SourceDocument]:
        documents = self.discover_source_documents(
            filing,
            raw_dir,
            include_exhibits=include_exhibits,
            exhibit_types=exhibit_types,
            max_exhibits_per_filing=max_exhibits_per_filing,
        )
        target_dir = raw_dir / filing.ticker / filing.accession_no_dashes()
        target_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[SourceDocument] = []
        for document in documents:
            if not document.document_url:
                continue
            target_path = target_dir / document.document
            if target_path.exists() and target_path.stat().st_size > 0:
                data = target_path.read_bytes()
            else:
                data = self.get_bytes(document.document_url)
                target_path.write_bytes(data)
            document.local_path = str(target_path)
            document.sha256 = hashlib.sha256(data).hexdigest()
            if document.is_primary:
                filing.local_path = document.local_path
                filing.sha256 = document.sha256
                filing.primary_document_url = document.document_url
                filing.primary_document = document.document
            downloaded.append(document)
        (target_dir / "metadata.json").write_text(
            json.dumps(filing.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (target_dir / "source_documents.json").write_text(
            json.dumps([document.to_dict() for document in downloaded], indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return downloaded


def _get_recent_value(recent: dict[str, list[Any]], key: str, idx: int) -> str:
    values = recent.get(key, [])
    if idx >= len(values):
        return ""
    value = values[idx]
    return "" if value is None else str(value)


def select_form_balanced_filings(
    candidates: list[FilingRecord],
    forms: set[str],
    max_per_form: int,
) -> list[FilingRecord]:
    grouped: dict[str, list[FilingRecord]] = {form: [] for form in forms}
    for filing in candidates:
        grouped.setdefault(filing.form, []).append(filing)

    ordered_forms = [form for form in FORM_PRIORITY if form in forms]
    ordered_forms.extend(sorted(forms - set(ordered_forms)))

    selected: list[FilingRecord] = []
    seen: set[str] = set()
    for form in ordered_forms:
        for filing in grouped.get(form, [])[:max_per_form]:
            if filing.accession_number in seen:
                continue
            selected.append(filing)
            seen.add(filing.accession_number)
    return selected


def parse_filing_detail_rows(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, str]] = []
    for table in soup.find_all("table", class_="tableFile"):
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 5:
                continue
            document_cell = cells[2]
            link = document_cell.find("a")
            if link and link.get("href"):
                document = Path(str(link.get("href"))).name
            else:
                document = document_cell.get_text(" ", strip=True).split(" ")[0]
            document = document.strip()
            if not document:
                continue
            rows.append(
                {
                    "sequence": cells[0].get_text(" ", strip=True),
                    "description": cells[1].get_text(" ", strip=True),
                    "document": document,
                    "document_type": cells[3].get_text(" ", strip=True).upper(),
                    "size": cells[4].get_text(" ", strip=True),
                }
            )
    return rows


def build_source_documents(
    filing: FilingRecord,
    rows: list[dict[str, str]],
    include_exhibits: bool = True,
    exhibit_types: tuple[str, ...] = DEFAULT_EXHIBIT_TYPES,
    max_exhibits_per_filing: int = 8,
) -> list[SourceDocument]:
    requested = {normalize_exhibit_type(item) for item in exhibit_types}
    selected: list[SourceDocument] = []
    exhibit_count = 0
    for row in sorted(rows, key=detail_row_sort_key):
        document = str(row.get("document", "")).strip()
        if not document or not is_text_document(document):
            continue
        classification = classify_archive_document(row, primary_document=filing.primary_document)
        if classification is None:
            continue
        is_primary = classification == "PRIMARY"
        if not is_primary:
            if not include_exhibits or not exhibit_type_requested(classification, requested):
                continue
            if exhibit_count >= max_exhibits_per_filing:
                continue
            exhibit_count += 1
        selected.append(source_document_from_row(filing, row, is_primary=is_primary, document_type=classification))
    return selected


def classify_archive_document(row: dict[str, str], primary_document: str = "") -> str | None:
    document = str(row.get("document", "")).strip()
    document_lower = document.lower()
    document_type = normalize_exhibit_type(str(row.get("document_type", "")).strip())
    description = str(row.get("description", "")).lower()
    if not is_text_document(document):
        return None
    if document_lower == primary_document.lower():
        return "PRIMARY"
    if document_type.startswith("EX-101") or document_type in SKIPPED_ARCHIVE_TYPES:
        return None
    if any(document_lower.endswith(suffix) for suffix in (".xml", ".xsd", ".jpg", ".jpeg", ".png", ".gif", ".zip")):
        return None
    if is_exhibit_type(document_type, "EX-99") or filename_suggests_exhibit(document_lower, "99"):
        if document_type.startswith("EX-") and not is_exhibit_type(document_type, "EX-99"):
            return None
        return "EX-99.1" if "99.1" in document_type or "991" in document_lower or "99-1" in document_lower else "EX-99"
    if is_exhibit_type(document_type, "EX-21") or filename_suggests_exhibit(document_lower, "21"):
        if document_type.startswith("EX-") and not is_exhibit_type(document_type, "EX-21"):
            return None
        return "EX-21"
    if is_exhibit_type(document_type, "EX-10") or filename_suggests_exhibit(document_lower, "10"):
        if document_type.startswith("EX-") and not is_exhibit_type(document_type, "EX-10"):
            return None
        return "EX-10"
    if "complete submission text file" in description:
        return None
    return None


def source_document_from_row(
    filing: FilingRecord,
    row: dict[str, str],
    is_primary: bool,
    document_type: str | None = None,
) -> SourceDocument:
    document = str(row.get("document", "")).strip()
    return SourceDocument(
        ticker=filing.ticker,
        cik=filing.cik,
        company_name=filing.company_name,
        form=filing.form,
        accession_number=filing.accession_number,
        filing_date=filing.filing_date,
        report_date=filing.report_date,
        accepted_timestamp=filing.accepted_timestamp,
        archive_url=filing.archive_url,
        document=document,
        document_type=document_type or ("PRIMARY" if is_primary else normalize_exhibit_type(str(row.get("document_type", "")))),
        description=str(row.get("description", "")).strip(),
        sequence=str(row.get("sequence", "")).strip(),
        document_url=f"{filing.archive_url}{document}" if document else "",
        is_primary=is_primary,
    )


def primary_document_row(filing: FilingRecord) -> dict[str, str]:
    return {
        "sequence": "1",
        "description": "primary document",
        "document": filing.primary_document,
        "document_type": filing.form,
        "size": "",
    }


def normalize_exhibit_type(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().upper())


def is_text_document(document: str) -> bool:
    return Path(document.lower()).suffix in TEXT_DOCUMENT_EXTENSIONS


def is_exhibit_type(document_type: str, prefix: str) -> bool:
    if not document_type.startswith(prefix):
        return False
    remainder = document_type[len(prefix) :]
    return not remainder or not remainder[0].isdigit()


def filename_suggests_exhibit(document_lower: str, exhibit_number: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", document_lower)
    if exhibit_number == "10" and compact.startswith(("ex101", "exhibit101")):
        return False
    return bool(
        re.search(rf"(?:^|[^a-z0-9])ex(?:hibit)?[-_ ]?{exhibit_number}(?:[._-]?\d|[^0-9]|$)", document_lower)
        or f"exhibit{exhibit_number}" in compact
        or f"ex{exhibit_number}" in compact
    )


def exhibit_type_requested(document_type: str, requested: set[str]) -> bool:
    if document_type in requested:
        return True
    for prefix in ("EX-10", "EX-21", "EX-99"):
        if prefix in requested and is_exhibit_type(document_type, prefix):
            return True
    return False


def infer_document_type(document: str) -> str:
    document_lower = document.lower()
    for exhibit_number, document_type in [("99", "EX-99"), ("21", "EX-21"), ("10", "EX-10")]:
        if filename_suggests_exhibit(document_lower, exhibit_number):
            return document_type
    return ""


def detail_row_sort_key(row: dict[str, str]) -> tuple[int, str]:
    sequence = str(row.get("sequence", "")).strip()
    try:
        sequence_value = int(sequence)
    except ValueError:
        sequence_value = 9999
    return (sequence_value, str(row.get("document", "")))
