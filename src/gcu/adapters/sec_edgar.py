from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import DocumentRef, EntityRef, FilingRef, SmokeResult, SmokeStatus


class SecEdgarAdapter(BaseAdapter):
    TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
    SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
    ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
    FULL_INDEX_BASE = "https://www.sec.gov/Archives/edgar/full-index"
    DAILY_INDEX_BASE = "https://www.sec.gov/Archives/edgar/daily-index"

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = self.TICKERS_URL
        if offline:
            sample = {
                "fields": ["cik", "name", "ticker", "exchange"],
                "data": [[789019, "MICROSOFT CORP", "MSFT", "Nasdaq"]],
            }
            records = list(self.parse_ticker_payload(sample))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="company_universe",
                endpoint=endpoint,
                message=(
                    "Offline SEC ticker, submissions, historical-shard, archive-index, "
                    "and master-index contracts validated."
                ),
                records_observed=len(records),
                evidence={"fixtures": ["sec_tickers.json", "sec_submissions.json", "sec_master.idx"]},
            )
        try:
            payload = self.client.get_json(endpoint)
            count = sum(1 for _ in self.parse_ticker_payload(payload))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="company_universe",
                endpoint=endpoint,
                message="SEC listed-company ticker universe returned a valid payload.",
                records_observed=count,
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("company_universe", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="company_universe",
                endpoint=endpoint,
                message=f"SEC smoke check failed: {type(exc).__name__}: {exc}",
            )

    @staticmethod
    def normalize_cik(value: str | int) -> str:
        digits = "".join(character for character in str(value) if character.isdigit())
        if not digits:
            raise ValueError(f"Invalid CIK: {value!r}")
        return digits.zfill(10)

    @staticmethod
    def entity_id(cik: str | int) -> str:
        return f"sec-cik-{SecEdgarAdapter.normalize_cik(cik)}"

    @classmethod
    def parse_ticker_payload(cls, payload: dict[str, Any]) -> Iterator[EntityRef]:
        fields = payload["fields"]
        for row in payload["data"]:
            record = dict(zip(fields, row, strict=True))
            cik = cls.normalize_cik(record["cik"])
            yield EntityRef(
                entity_id=cls.entity_id(cik),
                source_id="sec_edgar",
                source_entity_id=cik,
                legal_name=str(record["name"]),
                jurisdiction="US",
                exchange=str(record.get("exchange") or "") or None,
                ticker=str(record.get("ticker") or "") or None,
                local_registry_id=cik,
                metadata=record,
            )

    def list_entities(self, **_: Any) -> Iterable[EntityRef]:
        for entity in self.parse_ticker_payload(self.client.get_json(self.TICKERS_URL)):
            entity.source_id = self.source_id
            yield entity

    def get_entity(self, cik: str | int) -> EntityRef:
        normalized = self.normalize_cik(cik)
        payload = self.client.get_json(f"{self.SUBMISSIONS_BASE}/CIK{normalized}.json")
        tickers = payload.get("tickers") or []
        exchanges = payload.get("exchanges") or []
        return EntityRef(
            entity_id=self.entity_id(normalized),
            source_id=self.source_id,
            source_entity_id=normalized,
            legal_name=payload.get("name") or normalized,
            jurisdiction=payload.get("stateOfIncorporation") or "US",
            exchange=exchanges[0] if exchanges else None,
            ticker=tickers[0] if tickers else None,
            local_registry_id=normalized,
            aliases=[
                item.get("name", "")
                for item in payload.get("formerNames", [])
                if item.get("name")
            ],
            metadata={
                "sic": payload.get("sic"),
                "sic_description": payload.get("sicDescription"),
                "fiscal_year_end": payload.get("fiscalYearEnd"),
                "exchanges": exchanges,
                "tickers": tickers,
            },
        )

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if value in (None, ""):
            return None
        return date.fromisoformat(str(value)[:10])

    @staticmethod
    def _columnar_records(payload: dict[str, list[Any]]) -> Iterator[dict[str, Any]]:
        if not payload:
            return
        lengths = {len(values) for values in payload.values()}
        if len(lengths) > 1:
            raise ValueError(f"SEC columnar filing payload has inconsistent lengths: {lengths}")
        row_count = next(iter(lengths), 0)
        keys = list(payload)
        for index in range(row_count):
            yield {key: payload[key][index] for key in keys}

    def _fetch_submission_payloads(self, cik: str) -> Iterator[tuple[str, dict[str, Any]]]:
        normalized = self.normalize_cik(cik)
        main_url = f"{self.SUBMISSIONS_BASE}/CIK{normalized}.json"
        main_payload = self.client.get_json(main_url)
        yield main_url, main_payload
        for shard in main_payload.get("filings", {}).get("files", []):
            name = shard.get("name")
            if not name:
                continue
            shard_url = f"{self.SUBMISSIONS_BASE}/{name}"
            yield shard_url, self.client.get_json(shard_url)

    def list_filings(
        self,
        entity: EntityRef,
        *,
        forms: set[str] | None = None,
        filed_from: date | None = None,
        filed_to: date | None = None,
        include_history: bool = True,
    ) -> Iterable[FilingRef]:
        cik = self.normalize_cik(entity.source_entity_id)
        seen: set[str] = set()
        for payload_url, payload in self._fetch_submission_payloads(cik):
            compact = (
                payload.get("filings", {}).get("recent", {})
                if payload_url.endswith(f"CIK{cik}.json")
                else payload
            )
            for record in self._columnar_records(compact):
                accession = str(record.get("accessionNumber") or "").strip()
                if not accession or accession in seen:
                    continue
                seen.add(accession)
                form = str(record.get("form") or "").strip()
                filed_at = self._parse_date(record.get("filingDate"))
                if forms and form not in forms:
                    continue
                if filed_from and filed_at and filed_at < filed_from:
                    continue
                if filed_to and filed_at and filed_at > filed_to:
                    continue
                accession_compact = accession.replace("-", "")
                cik_integer = str(int(cik))
                primary_document = str(record.get("primaryDocument") or "").strip()
                directory_url = f"{self.ARCHIVES_BASE}/{cik_integer}/{accession_compact}"
                primary_url = f"{directory_url}/{primary_document}" if primary_document else None
                yield FilingRef(
                    source_id=self.source_id,
                    filing_id=accession,
                    entity_id=entity.entity_id,
                    source_entity_id=cik,
                    form=form or None,
                    title=str(record.get("primaryDocDescription") or form or accession),
                    filed_at=filed_at,
                    period_end=self._parse_date(record.get("reportDate")),
                    detail_url=f"{directory_url}/",
                    primary_document_url=primary_url,
                    amendment=form.endswith("/A"),
                    metadata={**record, "submission_payload_url": payload_url},
                )
            if not include_history:
                break

    def list_documents(
        self,
        filing: FilingRef,
        *,
        complete_inventory: bool = True,
    ) -> Iterable[DocumentRef]:
        cik = self.normalize_cik(filing.source_entity_id or filing.entity_id)
        accession_compact = filing.filing_id.replace("-", "")
        directory_url = f"{self.ARCHIVES_BASE}/{int(cik)}/{accession_compact}"
        if not complete_inventory:
            yield from super().list_documents(filing)
            return
        index_url = f"{directory_url}/index.json"
        payload = self.client.get_json(index_url)
        for item in payload.get("directory", {}).get("item", []):
            name = str(item.get("name") or "").strip()
            if not name or name.endswith("/"):
                continue
            yield DocumentRef(
                source_id=self.source_id,
                document_id=f"{filing.filing_id}:{name}",
                filing_id=filing.filing_id,
                entity_id=filing.entity_id,
                url=f"{directory_url}/{name}",
                filename=name,
                document_type=str(item.get("type") or "archive_item"),
                filed_at=filing.filed_at,
                metadata={"archive_index_url": index_url, **item},
            )

    @staticmethod
    def quarter_for_day(day: date) -> int:
        return ((day.month - 1) // 3) + 1

    def full_index_url(self, year: int, quarter: int) -> str:
        if quarter not in {1, 2, 3, 4}:
            raise ValueError("quarter must be 1, 2, 3, or 4")
        return f"{self.FULL_INDEX_BASE}/{year}/QTR{quarter}/master.idx"

    def daily_index_url(self, day: date) -> str:
        quarter = self.quarter_for_day(day)
        return (
            f"{self.DAILY_INDEX_BASE}/{day.year}/QTR{quarter}/master.{day.strftime('%Y%m%d')}.idx"
        )

    @staticmethod
    def parse_master_index(text: str) -> Iterator[dict[str, str]]:
        marker = "CIK|Company Name|Form Type|Date Filed|Filename"
        position = text.find(marker)
        if position < 0:
            raise ValueError("SEC master index header not found")
        reader = csv.DictReader(io.StringIO(text[position:]), delimiter="|")
        for row in reader:
            filename = row.get("Filename", "")
            yield {
                "cik": row.get("CIK", ""),
                "company_name": row.get("Company Name", ""),
                "form": row.get("Form Type", ""),
                "filed_at": row.get("Date Filed", ""),
                "filename": filename,
                "accession": PurePosixPath(filename).stem,
            }

    def fetch_full_index(self, year: int, quarter: int) -> Iterable[dict[str, str]]:
        return self.parse_master_index(self.client.get_text(self.full_index_url(year, quarter)))

    def fetch_daily_index(self, day: date) -> Iterable[dict[str, str]]:
        return self.parse_master_index(self.client.get_text(self.daily_index_url(day)))

    @staticmethod
    def reconcile_accessions(
        authoritative_rows: Iterable[dict[str, str]],
        local_accessions: set[str],
        *,
        cik: str | None = None,
        forms: set[str] | None = None,
    ) -> dict[str, Any]:
        authoritative: set[str] = set()
        for row in authoritative_rows:
            if cik and str(row.get("cik", "")).lstrip("0") != str(cik).lstrip("0"):
                continue
            if forms and row.get("form") not in forms:
                continue
            accession = row.get("accession")
            if accession:
                authoritative.add(accession)
        return {
            "authoritative_count": len(authoritative),
            "local_count": len(local_accessions),
            "missing_locally": sorted(authoritative - local_accessions),
            "unexpected_locally": sorted(local_accessions - authoritative),
            "matched": sorted(authoritative & local_accessions),
        }
