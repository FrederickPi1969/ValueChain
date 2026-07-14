from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

from gcu.models import EntityRef
from valuechain.acquisition_state import AcquisitionIssuer
from valuechain.ad_hoc_state import AdHocRequestState
from valuechain.async_global_acquisition import AsyncGlobalAcquisitionRunner
from valuechain.async_sec_acquisition import AsyncSecAcquisitionRunner
from valuechain.disclosure_resolver import (
    ResolveDisclosureRequest,
    row_matches_request,
)
from valuechain.global_acquisition import (
    CNINFO_SOURCE,
    OPENDART_SOURCE,
    GlobalAcquisitionConfig,
)
from valuechain.global_acquisition_state import GlobalSourceAcquisitionState
from valuechain.opendart_acquisition import OpenDartAcquisitionRunner
from valuechain.postgres_acquisition_state import PostgresAcquisitionState
from valuechain.sec_acquisition import (
    SEC_DATA_BASE,
    AcquisitionConfig,
    parse_submission_columns,
    parse_submission_rows,
)


class AdHocNotFound(Exception):
    pass


class AdHocUnsupported(Exception):
    pass


def _filing_window(request: ResolveDisclosureRequest) -> tuple[date, date]:
    if str(request.year_basis) == "filing_date" or str(request.document_type) in {
        "current_report",
        "material_event",
    }:
        return date(request.year, 1, 1), date(request.year, 12, 31)
    return date(request.year, 1, 1), date(request.year + 1, 12, 31)


def _candidate_row(
    source_id: str,
    form_raw: str,
    filing_date: Any,
    report_date: Any = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "form_raw": form_raw,
        "filing_date": filing_date,
        "report_date": report_date,
        "metadata": metadata or {},
    }


class AdHocAcquisitionWorker:
    def __init__(
        self,
        database_url: str,
        repository_root: Path,
        sec_config: AcquisitionConfig | None = None,
        global_config: GlobalAcquisitionConfig | None = None,
        max_attempts: int = 5,
    ) -> None:
        self.database_url = database_url
        self.repository_root = repository_root
        self.sec_config = sec_config or AcquisitionConfig.from_env()
        self.global_config = global_config or GlobalAcquisitionConfig.from_env()
        self.max_attempts = max_attempts

    async def run_once(self) -> dict[str, Any] | None:
        with AdHocRequestState(self.database_url) as state:
            state.recover_stale()
            job = state.claim()
        if job is None:
            return None
        request = ResolveDisclosureRequest.model_validate(job["request_payload"])
        try:
            document_ids = await self._dispatch(job, request)
            if not document_ids:
                raise AdHocNotFound("The upstream source returned no matching document")
            result = {
                "source_id": job["source_id"],
                "source_issuer_id": job["source_issuer_id"],
                "document_ids": document_ids,
                "retrieval": "upstream_api",
            }
            with AdHocRequestState(self.database_url, ensure_schema=False) as state:
                state.complete(job["request_id"], document_ids, result)
            return {"request_id": str(job["request_id"]), "status": "complete", **result}
        except AdHocNotFound as exc:
            with AdHocRequestState(self.database_url, ensure_schema=False) as state:
                state.finish_without_result(
                    job["request_id"], "not_found", "upstream_not_found", str(exc)
                )
            return {"request_id": str(job["request_id"]), "status": "not_found"}
        except AdHocUnsupported as exc:
            with AdHocRequestState(self.database_url, ensure_schema=False) as state:
                state.finish_without_result(
                    job["request_id"], "unsupported", "unsupported_source", str(exc)
                )
            return {"request_id": str(job["request_id"]), "status": "unsupported"}
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            with AdHocRequestState(self.database_url, ensure_schema=False) as state:
                if int(job["attempts"]) >= self.max_attempts:
                    state.finish_without_result(
                        job["request_id"], "failed", "upstream_error", error
                    )
                    status = "failed"
                else:
                    delay = min(30 * 2 ** (int(job["attempts"]) - 1), 900)
                    state.retry(job["request_id"], "upstream_error", error, delay)
                    status = "retry"
            return {
                "request_id": str(job["request_id"]),
                "status": status,
                "error": error,
            }

    async def run_forever(self, idle_seconds: float = 2.0) -> None:
        while True:
            result = await self.run_once()
            if result is None:
                await asyncio.sleep(idle_seconds)

    async def _dispatch(
        self, job: dict[str, Any], request: ResolveDisclosureRequest
    ) -> list[int]:
        source_id = str(job["source_id"])
        if source_id == "sec_edgar":
            return await self._sec(job, request)
        if source_id == CNINFO_SOURCE:
            return await self._cninfo(job, request)
        if source_id == OPENDART_SOURCE:
            return await self._opendart(job, request)
        raise AdHocUnsupported(f"No on-demand connector is configured for {source_id}")

    def _issuer(self, source_id: str, source_issuer_id: str) -> dict[str, Any]:
        with AdHocRequestState(self.database_url, ensure_schema=False) as state:
            row = state.connection.execute(
                """
                SELECT * FROM acquisition_issuers
                WHERE source_id = %s AND source_issuer_id = %s
                """,
                (source_id, source_issuer_id),
            ).fetchone()
        if not row:
            raise AdHocNotFound("Company is not present in the source issuer registry")
        return dict(row)

    def _document_ids(self, source_id: str, filing_ids: list[str]) -> list[int]:
        if not filing_ids:
            return []
        with AdHocRequestState(self.database_url, ensure_schema=False) as state:
            rows = state.connection.execute(
                """
                SELECT document_id FROM acquisition_documents
                WHERE source_id = %s AND source_filing_id = ANY(%s)
                  AND status = 'complete'
                ORDER BY document_id
                """,
                (source_id, filing_ids),
            ).fetchall()
        return [int(row["document_id"]) for row in rows]

    async def _sec(
        self, job: dict[str, Any], request: ResolveDisclosureRequest
    ) -> list[int]:
        issuer_row = self._issuer("sec_edgar", str(job["source_issuer_id"]))
        cik = str(job["source_issuer_id"]).zfill(10)
        begin, end = _filing_window(request)
        runner = AsyncSecAcquisitionRunner(self.sec_config, self.repository_root)
        await runner.schema_guard.prepare()
        async with await runner._new_client() as client:
            payload = await client.get_json(f"{SEC_DATA_BASE}/submissions/CIK{cik}.json")
            filings = parse_submission_rows(
                payload, cik=cik, start_date=begin.isoformat(), end_date=end.isoformat()
            )
            for history in payload.get("filings", {}).get("files", []):
                filing_from = str(history.get("filingFrom") or "")
                filing_to = str(history.get("filingTo") or "")
                if filing_to and filing_to < begin.isoformat():
                    continue
                if filing_from and filing_from > end.isoformat():
                    continue
                filename = str(history.get("name") or "")
                if filename:
                    historical = await client.get_json(
                        f"{SEC_DATA_BASE}/submissions/{filename}"
                    )
                    filings.extend(
                        parse_submission_columns(
                            historical,
                            cik=cik,
                            start_date=begin.isoformat(),
                            end_date=end.isoformat(),
                        )
                    )
            selected = {
                row["accession_number"]: row
                for row in filings
                if row_matches_request(
                    _candidate_row(
                        "sec_edgar",
                        row["form"],
                        row["filing_date"],
                        row.get("report_date"),
                    ),
                    request,
                )
            }
            if not selected:
                raise AdHocNotFound(
                    f"SEC returned no {request.document_type} for {issuer_row['company_name']} in {request.year}"
                )
            with AdHocRequestState(self.database_url, ensure_schema=False) as request_state:
                request_state.mark_downloading(job["request_id"])
            with PostgresAcquisitionState(
                self.database_url, "sec_edgar", False
            ) as acquisition_state:
                complete = acquisition_state.complete_filing_ids(selected)
                for accession, filing in sorted(selected.items()):
                    if accession not in complete:
                        await runner.acquire_filing(acquisition_state, client, filing)
        return self._document_ids("sec_edgar", list(selected))

    async def _cninfo(
        self, job: dict[str, Any], request: ResolveDisclosureRequest
    ) -> list[int]:
        issuer_row = self._issuer(CNINFO_SOURCE, str(job["source_issuer_id"]))
        entity = EntityRef(
            entity_id=f"cninfo-{issuer_row['source_issuer_id']}",
            source_id=CNINFO_SOURCE,
            source_entity_id=issuer_row["source_issuer_id"],
            legal_name=issuer_row["company_name"],
            exchange=issuer_row["exchange"] or None,
            ticker=issuer_row["ticker"] or None,
            metadata=issuer_row.get("metadata") or {},
        )
        runner = AsyncGlobalAcquisitionRunner(CNINFO_SOURCE, self.global_config)
        begin, end = _filing_window(request)
        years = range(begin.year, end.year + 1)
        async with await runner._new_client() as client:
            filings = []
            for year in years:
                filings.extend(await runner._list_cninfo_filings(client, entity, year))
            selected = {
                filing.filing_id: filing
                for filing in filings
                if row_matches_request(
                    _candidate_row(
                        CNINFO_SOURCE,
                        filing.form or "",
                        filing.filed_at,
                        filing.period_end,
                        {**filing.metadata, "title": filing.title, "amendment": filing.amendment},
                    ),
                    request,
                )
            }
            if not selected:
                raise AdHocNotFound(
                    f"CNINFO returned no {request.document_type} for {issuer_row['company_name']} in {request.year}"
                )
            with AdHocRequestState(self.database_url, ensure_schema=False) as request_state:
                request_state.mark_downloading(job["request_id"])
            with GlobalSourceAcquisitionState(
                self.database_url, CNINFO_SOURCE, False
            ) as acquisition_state:
                acquisition_state.upsert_filings(selected.values(), self.global_config.raw_root)
                complete = acquisition_state.complete_filing_ids(selected)
                for filing_id, filing in selected.items():
                    if filing_id not in complete:
                        await runner._download_cninfo_filing(
                            acquisition_state, client, filing
                        )
        return self._document_ids(CNINFO_SOURCE, list(selected))

    async def _opendart(
        self, job: dict[str, Any], request: ResolveDisclosureRequest
    ) -> list[int]:
        issuer_row = self._issuer(OPENDART_SOURCE, str(job["source_issuer_id"]))
        runner = OpenDartAcquisitionRunner(self.global_config)
        begin, end = _filing_window(request)
        records: list[dict[str, Any]] = []
        try:
            async with await runner._new_client() as client:
                page = 1
                total_pages = 1
                while page <= total_pages:
                    payload = await client.get_json(
                        f"{runner.API_BASE}/list.json",
                        params={
                            "crtfc_key": runner.settings.opendart_api_key,
                            "corp_code": job["source_issuer_id"],
                            "bgn_de": begin.strftime("%Y%m%d"),
                            "end_de": end.strftime("%Y%m%d"),
                            "page_no": page,
                            "page_count": 100,
                            "sort": "date",
                            "sort_mth": "desc",
                        },
                    )
                    status = str(payload.get("status") or "")
                    if status == "013":
                        break
                    if status != "000":
                        raise RuntimeError(
                            f"OpenDART list error {status}: {payload.get('message')}"
                        )
                    records.extend(payload.get("list") or [])
                    total_pages = max(1, int(payload.get("total_page") or 1))
                    page += 1
                entities, filings = runner._convert_records(records)
                selected = {
                    filing.filing_id: filing
                    for filing in filings
                    if row_matches_request(
                        _candidate_row(
                            OPENDART_SOURCE,
                            filing.form or "",
                            filing.filed_at,
                            filing.period_end,
                            {**filing.metadata, "title": filing.title, "amendment": filing.amendment},
                        ),
                        request,
                    )
                }
                if not selected:
                    raise AdHocNotFound(
                        f"OpenDART returned no {request.document_type} for {issuer_row['company_name']} in {request.year}"
                    )
                with AdHocRequestState(self.database_url, ensure_schema=False) as request_state:
                    request_state.mark_downloading(job["request_id"])
                with GlobalSourceAcquisitionState(
                    self.database_url, OPENDART_SOURCE, False
                ) as acquisition_state:
                    acquisition_state.upsert_entities(entities, priority=100)
                    acquisition_state.upsert_filings(
                        selected.values(), self.global_config.raw_root
                    )
                    complete = acquisition_state.complete_filing_ids(selected)
                    for filing_id in selected:
                        if filing_id in complete:
                            continue
                        row = acquisition_state.connection.execute(
                            """
                            SELECT * FROM acquisition_filings
                            WHERE source_id = %s AND source_filing_id = %s
                            """,
                            (OPENDART_SOURCE, filing_id),
                        ).fetchone()
                        await runner._download_filing(
                            acquisition_state, client, dict(row)
                        )
        finally:
            runner.close()
        return self._document_ids(OPENDART_SOURCE, list(selected))


async def run_ad_hoc_worker() -> None:
    from valuechain.config import Settings

    settings = Settings()
    worker = AdHocAcquisitionWorker(
        database_url=settings.database_url,
        repository_root=Path.cwd(),
    )
    await worker.run_forever()


def main() -> None:
    asyncio.run(run_ad_hoc_worker())


if __name__ == "__main__":
    main()
