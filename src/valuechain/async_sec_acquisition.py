from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from valuechain.acquisition_schema import AcquisitionSchemaGuard
from valuechain.acquisition_schedule import (
    choose_issuer_scan_plan,
    years_with_current,
)
from valuechain.acquisition_state import AcquisitionIssuer
from valuechain.async_http import AdaptiveRateLimiter, AsyncHttpClient
from valuechain.postgres_acquisition_state import PostgresAcquisitionState
from valuechain.proxy_pool import ProxyPoolClient
from valuechain.sec_acquisition import (
    SEC_DATA_BASE,
    AcquisitionConfig,
    SecAcquisitionRunner,
    atomic_write_json,
    load_priority_tickers,
    parse_company_universe,
    parse_submission_columns,
    parse_submission_rows,
)


class AsyncSecAcquisitionRunner:
    """Bounded asynchronous SEC backfill with one proxy and DB connection per worker."""

    def __init__(self, config: AcquisitionConfig, repository_root: Path) -> None:
        self.config = config
        self.repository_root = repository_root
        self.proxy_pool = ProxyPoolClient(config.proxy_pool_url)
        self.limiter = AdaptiveRateLimiter(config.requests_per_second)
        self.schema_guard = AcquisitionSchemaGuard(config.database_url)
        self.config.raw_root.mkdir(parents=True, exist_ok=True)

    async def _new_client(self) -> AsyncHttpClient:
        return await AsyncHttpClient.create(
            proxy_pool=self.proxy_pool,
            limiter=self.limiter,
            user_agent=self.config.sec_user_agent,
            timeout_seconds=self.config.request_timeout_seconds,
            max_retries=self.config.request_retries,
        )

    async def refresh_universe(self) -> int:
        await self.schema_guard.prepare()
        await asyncio.to_thread(self.proxy_pool.health)
        async with await self._new_client() as client:
            payload = await client.get_json(
                "https://www.sec.gov/files/company_tickers_exchange.json"
            )
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        catalog_path = (
            self.config.raw_root
            / "sec_edgar"
            / "_catalog"
            / f"company_tickers_exchange.{timestamp}.json"
        )
        await asyncio.to_thread(atomic_write_json, catalog_path, payload)
        priority_tickers = load_priority_tickers(
            self.repository_root / "data" / "universe" / "ai_infra_universe.csv"
        )
        issuers = parse_company_universe(payload, priority_tickers)
        with PostgresAcquisitionState(
            self.config.database_url, ensure_schema=False
        ) as state:
            count = state.upsert_issuers(issuers)
            state.ensure_scan_years(
                years_with_current(self.config.target_years, datetime.now(UTC).year)
            )
        return count

    async def run_batch(self) -> dict[str, object]:
        await self.schema_guard.prepare()
        counts = {"issuers": 0, "filings": 0, "documents": 0, "errors": 0}
        sync_runner = SecAcquisitionRunner(self.config, self.repository_root)
        with PostgresAcquisitionState(
            self.config.database_url, ensure_schema=False
        ) as state:
            stats = state.stats()
            refresh_due = not stats["issuers"] or sync_runner.universe_refresh_due()
        if refresh_due:
            await self.refresh_universe()

        with PostgresAcquisitionState(
            self.config.database_url, ensure_schema=False
        ) as state:
            current_year = datetime.now(UTC).year
            years = years_with_current(self.config.target_years, current_year)
            state.ensure_scan_years(years)
            plan = choose_issuer_scan_plan(
                state,
                years=years,
                current_year=current_year,
                rescan_hours=self.config.rescan_hours,
            )
            filing_year = plan.filing_year
            mode = plan.mode
            run_id = datetime.now(UTC).strftime(f"sec-{filing_year}-%Y%m%dT%H%M%S.%fZ")
            state.begin_run(run_id, filing_year, mode)
            issuers = state.claim_issuers(
                self.config.issuer_limit,
                filing_year=filing_year,
                rescan_hours=plan.rescan_hours,
            )

        queue: asyncio.Queue[AcquisitionIssuer] = asyncio.Queue()
        for issuer in issuers:
            queue.put_nowait(issuer)
        worker_count = min(self.config.request_concurrency, len(issuers))
        if worker_count:
            await asyncio.gather(
                *(
                    self._worker(queue, filing_year, counts)
                    for _ in range(worker_count)
                )
            )

        status = "complete" if counts["errors"] == 0 else "partial"
        with PostgresAcquisitionState(
            self.config.database_url, ensure_schema=False
        ) as state:
            state.finish_run(run_id, status, counts)
            final_stats = state.stats()
        return {
            "run_id": run_id,
            "target_year": filing_year,
            "mode": mode,
            "status": status,
            "counts": counts,
            "worker_count": worker_count,
            "effective_rps": round(self.limiter.current_rate, 3),
            "state": final_stats,
        }

    async def _worker(
        self,
        queue: asyncio.Queue[AcquisitionIssuer],
        filing_year: int,
        counts: dict[str, int],
    ) -> None:
        state = await asyncio.to_thread(
            PostgresAcquisitionState,
            self.config.database_url,
            "sec_edgar",
            False,
        )
        try:
            async with await self._new_client() as client:
                while not queue.empty():
                    try:
                        issuer = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    counts["issuers"] += 1
                    try:
                        result = await self.acquire_issuer(
                            state, client, issuer, filing_year
                        )
                        counts["filings"] += result["filings"]
                        counts["documents"] += result["documents"]
                        await asyncio.to_thread(
                            state.complete_issuer,
                            issuer.cik,
                            filing_year,
                        )
                    except Exception as exc:  # noqa: BLE001
                        counts["errors"] += 1
                        await asyncio.to_thread(
                            state.fail_issuer,
                            issuer.cik,
                            f"{type(exc).__name__}: {exc}",
                            filing_year,
                        )
                    finally:
                        queue.task_done()
        finally:
            await asyncio.to_thread(state.close)

    async def acquire_issuer(
        self,
        state: PostgresAcquisitionState,
        client: AsyncHttpClient,
        issuer: AcquisitionIssuer,
        filing_year: int,
    ) -> dict[str, int]:
        payload = await client.get_json(
            f"{SEC_DATA_BASE}/submissions/CIK{issuer.cik}.json"
        )
        start_date = f"{filing_year}-01-01"
        end_date = f"{filing_year}-12-31"
        filings = parse_submission_rows(
            payload,
            cik=issuer.cik,
            start_date=start_date,
            end_date=end_date,
        )
        for history in payload.get("filings", {}).get("files", []):
            filing_from = str(history.get("filingFrom", ""))
            filing_to = str(history.get("filingTo", ""))
            if filing_to and filing_to < start_date:
                continue
            if filing_from and filing_from > end_date:
                continue
            name = str(history.get("name", ""))
            if not name:
                continue
            historical = await client.get_json(f"{SEC_DATA_BASE}/submissions/{name}")
            filings.extend(
                parse_submission_columns(
                    historical,
                    cik=issuer.cik,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
        unique = {row["accession_number"]: row for row in filings}
        document_count = 0
        for filing in sorted(unique.values(), key=lambda row: row["filing_date"]):
            document_count += await self.acquire_filing(state, client, filing)
        return {"filings": len(unique), "documents": document_count}

    async def acquire_filing(
        self,
        state: PostgresAcquisitionState,
        client: AsyncHttpClient,
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
        await asyncio.to_thread(
            state.upsert_filing, filing, local_dir, "downloading"
        )
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
                None,
            ),
        ]
        if filing.get("primary_document"):
            documents.append(
                (
                    "primary_document",
                    f"{filing['archive_url']}{filing['primary_document']}",
                    local_dir / filing["primary_document"],
                    None,
                )
            )
        manifest_documents: list[dict[str, Any]] = []
        try:
            for kind, url, path, expected_media_type in documents:
                result = await client.download(
                    url,
                    path,
                    expected_media_type=expected_media_type,
                )
                result.update(
                    {
                        "accession_number": filing["accession_number"],
                        "document_kind": kind,
                    }
                )
                await asyncio.to_thread(state.upsert_document, result)
                manifest_documents.append(result)
            await asyncio.to_thread(
                atomic_write_json,
                local_dir / "filing.json",
                {
                    "source_id": "sec_edgar",
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "filing": filing,
                    "documents": manifest_documents,
                },
            )
            await asyncio.to_thread(state.upsert_filing, filing, local_dir, "complete")
        except Exception as exc:
            await asyncio.to_thread(
                state.upsert_filing,
                filing,
                local_dir,
                "retry",
                f"{type(exc).__name__}: {exc}",
            )
            raise
        return len(manifest_documents)
