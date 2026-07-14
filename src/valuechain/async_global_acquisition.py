from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from gcu.adapters.filings_xbrl import FilingsXbrlAdapter
from gcu.config import Settings
from gcu.models import EntityRef, FilingRef
from gcu_priority_markets.adapters.cninfo import CninfoAdapter
from gcu_priority_markets.adapters.esef import PriorityEsefAdapter
from gcu_priority_markets.registry import PatchRegistry
from valuechain.acquisition_schema import AcquisitionSchemaGuard
from valuechain.acquisition_schedule import (
    choose_issuer_scan_plan,
    years_with_current,
)
from valuechain.acquisition_state import AcquisitionIssuer
from valuechain.async_http import AdaptiveRateLimiter, AsyncHttpClient
from valuechain.global_acquisition import (
    CNINFO_SOURCE,
    ESEF_SOURCE,
    GlobalAcquisitionConfig,
    is_report_summary,
    safe_filename,
    write_manifest,
)
from valuechain.global_acquisition_state import (
    GlobalSourceAcquisitionState,
    filing_local_dir,
)
from valuechain.postgres_acquisition_state import PostgresAcquisitionState
from valuechain.proxy_pool import ProxyPoolClient
from valuechain.sec_acquisition import atomic_write_json


class AsyncGlobalAcquisitionRunner:
    """Asynchronous hot paths for CNINFO and ESEF backfills."""

    def __init__(self, source_id: str, config: GlobalAcquisitionConfig) -> None:
        if source_id not in {CNINFO_SOURCE, ESEF_SOURCE}:
            raise ValueError(f"Async global acquisition does not support {source_id}")
        self.source_id = source_id
        self.config = config
        self.settings = Settings()
        if not self.settings.proxy_pool_url:
            raise RuntimeError("VALUECHAIN_PROXY_POOL_URL is required")
        self.proxy_pool = ProxyPoolClient(self.settings.proxy_pool_url)
        self.definition = PatchRegistry().get(source_id)
        self.schema_guard = AcquisitionSchemaGuard(config.database_url)
        target_rps = (
            config.cninfo_requests_per_second
            if source_id == CNINFO_SOURCE
            else config.esef_requests_per_second
        )
        self.limiter = AdaptiveRateLimiter(target_rps)

    async def _new_client(self) -> AsyncHttpClient:
        return await AsyncHttpClient.create(
            proxy_pool=self.proxy_pool,
            limiter=self.limiter,
            user_agent=self.settings.user_agent,
            contact_email=self.settings.contact_email,
            timeout_seconds=self.settings.http_timeout_seconds,
            max_retries=self.settings.http_max_retries,
            verify_tls=self.settings.verify_tls,
        )

    async def run_batch(self) -> dict[str, Any]:
        await self.schema_guard.prepare()
        if self.source_id == CNINFO_SOURCE:
            return await self._run_cninfo_batch()
        return await self._run_esef_batch()

    async def _run_cninfo_batch(self) -> dict[str, Any]:
        counts = {"issuers": 0, "filings": 0, "documents": 0, "errors": 0}
        current_year = datetime.now(UTC).year
        years = years_with_current(self.config.target_years, current_year)
        if self._cninfo_universe_refresh_due():
            await self._refresh_cninfo_universe(years)
        with (
            GlobalSourceAcquisitionState(
                self.config.database_url, CNINFO_SOURCE, False
            ) as state,
            PostgresAcquisitionState(
                self.config.database_url, CNINFO_SOURCE, False
            ) as queue_state,
        ):
            state.ensure_source(self.definition)
            queue_state.ensure_scan_years(years)
            plan = choose_issuer_scan_plan(
                queue_state,
                years=years,
                current_year=current_year,
                rescan_hours=self.config.cninfo_rescan_hours,
            )
            year = plan.filing_year
            mode = plan.mode
            run_id = datetime.now(UTC).strftime(
                f"cninfo-{year}-%Y%m%dT%H%M%S.%fZ"
            )
            queue_state.begin_run(run_id, year, mode)
            issuers = queue_state.claim_issuers(
                self.config.cninfo_issuer_limit,
                filing_year=year,
                rescan_hours=plan.rescan_hours,
            )

        queue: asyncio.Queue[AcquisitionIssuer] = asyncio.Queue()
        for issuer in issuers:
            queue.put_nowait(issuer)
        worker_count = min(self.config.worker_count, len(issuers))
        if worker_count:
            await asyncio.gather(
                *(self._cninfo_worker(queue, year, counts) for _ in range(worker_count))
            )

        status = "complete" if counts["errors"] == 0 else "partial"
        with (
            PostgresAcquisitionState(
                self.config.database_url, CNINFO_SOURCE, False
            ) as queue_state,
            GlobalSourceAcquisitionState(
                self.config.database_url, CNINFO_SOURCE, False
            ) as state,
        ):
            queue_state.finish_run(run_id, status, counts)
            stats = state.stats()
        return {
            "source_id": CNINFO_SOURCE,
            "target_year": year,
            "mode": mode,
            "status": status,
            "counts": counts,
            "worker_count": worker_count,
            "effective_rps": round(self.limiter.current_rate, 3),
            "state": stats,
        }

    def _cninfo_universe_refresh_due(self) -> bool:
        catalog_dir = self.config.raw_root / CNINFO_SOURCE / "_catalog"
        snapshots = list(catalog_dir.glob("szse_stock.*.json"))
        if not snapshots:
            return True
        newest = max(path.stat().st_mtime for path in snapshots)
        age_seconds = datetime.now(UTC).timestamp() - newest
        return age_seconds >= self.config.discovery_refresh_hours * 3600

    async def _refresh_cninfo_universe(self, years: tuple[int, ...]) -> int:
        headers = {
            "Referer": "https://www.cninfo.com.cn/new/index",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        best_payload: Any = None
        best_entities: list[EntityRef] = []
        for _attempt in range(CninfoAdapter.MAX_UNIVERSE_ATTEMPTS):
            async with await self._new_client() as client:
                payload = await client.get_json(CninfoAdapter.UNIVERSE_URL, headers=headers)
            entities = list(CninfoAdapter.parse_universe(payload))
            if len(entities) > len(best_entities):
                best_payload = payload
                best_entities = entities
            if len(entities) >= CninfoAdapter.TARGET_UNIVERSE_ROWS:
                break
        if len(best_entities) < CninfoAdapter.MIN_UNIVERSE_ROWS:
            raise ValueError(
                f"CNINFO universe refresh produced only {len(best_entities)} issuers"
            )
        timestamp = datetime.now(UTC)
        catalog_path = (
            self.config.raw_root
            / CNINFO_SOURCE
            / "_catalog"
            / f"szse_stock.{timestamp.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        await asyncio.to_thread(atomic_write_json, catalog_path, best_payload)
        digest = await asyncio.to_thread(
            lambda: hashlib.sha256(catalog_path.read_bytes()).hexdigest()
        )
        with (
            GlobalSourceAcquisitionState(
                self.config.database_url, CNINFO_SOURCE, False
            ) as state,
            PostgresAcquisitionState(
                self.config.database_url, CNINFO_SOURCE, False
            ) as queue_state,
        ):
            state.ensure_source(self.definition)
            count = state.upsert_entities(best_entities)
            state.record_universe_snapshot(
                path=catalog_path,
                source_url=CninfoAdapter.UNIVERSE_URL,
                row_count=count,
                sha256=digest,
                retrieved_at=timestamp,
            )
            queue_state.ensure_scan_years(years)
        return count

    async def _cninfo_worker(
        self,
        queue: asyncio.Queue[AcquisitionIssuer],
        year: int,
        counts: dict[str, int],
    ) -> None:
        state = await asyncio.to_thread(
            GlobalSourceAcquisitionState,
            self.config.database_url,
            CNINFO_SOURCE,
            False,
        )
        queue_state = await asyncio.to_thread(
            PostgresAcquisitionState,
            self.config.database_url,
            CNINFO_SOURCE,
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
                        result = await self._acquire_cninfo_issuer(
                            state, client, issuer, year
                        )
                        counts["filings"] += result["filings"]
                        counts["documents"] += result["documents"]
                        await asyncio.to_thread(
                            queue_state.complete_issuer, issuer.cik, year
                        )
                    except Exception as exc:  # noqa: BLE001
                        counts["errors"] += 1
                        await asyncio.to_thread(
                            queue_state.fail_issuer,
                            issuer.cik,
                            f"{type(exc).__name__}: {exc}",
                            year,
                        )
                    finally:
                        queue.task_done()
        finally:
            await asyncio.to_thread(state.close)
            await asyncio.to_thread(queue_state.close)

    async def _acquire_cninfo_issuer(
        self,
        state: GlobalSourceAcquisitionState,
        client: AsyncHttpClient,
        issuer: AcquisitionIssuer,
        year: int,
    ) -> dict[str, int]:
        entity = EntityRef(
            entity_id=f"cninfo-{issuer.cik}",
            source_id=CNINFO_SOURCE,
            source_entity_id=issuer.cik,
            legal_name=issuer.company_name,
            exchange=issuer.exchange or None,
            ticker=issuer.ticker or None,
        )
        filings = await self._list_cninfo_filings(client, entity, year)
        filings = [row for row in filings if not is_report_summary(row.title or "")]
        unique = {row.filing_id: row for row in filings}
        complete_ids = await asyncio.to_thread(
            state.complete_filing_ids, unique.keys()
        )
        await asyncio.to_thread(
            state.upsert_filings, unique.values(), self.config.raw_root
        )
        documents = 0
        pending = [
            filing
            for filing_id, filing in unique.items()
            if filing_id not in complete_ids
        ]
        for filing in pending:
            documents += await self._download_cninfo_filing(state, client, filing)
        return {"filings": len(pending), "documents": documents}

    async def _list_cninfo_filings(
        self,
        client: AsyncHttpClient,
        entity: EntityRef,
        year: int,
    ) -> list[FilingRef]:
        market = CninfoAdapter.MIC_MARKETS.get(str(entity.exchange).upper())
        if not market:
            raise ValueError(f"Unsupported CNINFO exchange {entity.exchange}")
        headers = {
            "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
            "Origin": "https://www.cninfo.com.cn",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        output: list[FilingRef] = []
        for page in range(1, 6):
            data = {
                "pageNum": str(page),
                "pageSize": "30",
                "column": CninfoAdapter.MARKET_COLUMNS[market],
                "tabName": "fulltext",
                "plate": "",
                "stock": f"{entity.ticker or ''},{entity.source_entity_id}",
                "searchkey": "",
                "secid": "",
                "category": CninfoAdapter.FINANCIAL_REPORT_CATEGORIES,
                "trade": "",
                "seDate": f"{year}-01-01~{year}-12-31",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            payload = await client.post_json(
                CninfoAdapter.FILING_URL, data=data, headers=headers
            )
            rows = payload.get("announcements") or []
            output.extend(
                CninfoAdapter.parse_announcements(
                    payload, entity=entity, market=market
                )
            )
            total_pages = int(
                payload.get("totalpages") or payload.get("totalPages") or page
            )
            if not rows or page >= total_pages or payload.get("hasMore") is False:
                break
        return output

    async def _download_cninfo_filing(
        self,
        state: GlobalSourceAcquisitionState,
        client: AsyncHttpClient,
        filing: FilingRef,
    ) -> int:
        local_dir = filing_local_dir(
            self.config.raw_root,
            CNINFO_SOURCE,
            filing.filed_at.year,
            filing.source_entity_id,
            filing.filing_id,
        )
        documents: list[dict[str, Any]] = []
        try:
            if filing.primary_document_url:
                filename = safe_filename(
                    filing.primary_document_url, f"{filing.filing_id}.pdf"
                )
                result = await client.download(
                    filing.primary_document_url,
                    local_dir / filename,
                    expected_media_type="application/pdf",
                )
                result["metadata"] = {"async": True}
                await asyncio.to_thread(
                    state.upsert_document,
                    filing.filing_id,
                    filing.form or "primary",
                    result,
                )
                documents.append(result)
            await asyncio.to_thread(
                write_manifest,
                local_dir / "filing.json",
                {"filing": filing.model_dump(mode="json"), "documents": documents},
            )
            await asyncio.to_thread(state.complete_filing, filing.filing_id)
        except Exception as exc:
            await asyncio.to_thread(
                state.fail_filing,
                filing.filing_id,
                f"{type(exc).__name__}: {exc}",
            )
            raise
        return len(documents)

    async def _run_esef_batch(self) -> dict[str, Any]:
        counts = {"discovered": 0, "filings": 0, "documents": 0, "errors": 0}
        with GlobalSourceAcquisitionState(
            self.config.database_url, ESEF_SOURCE, False
        ) as state:
            state.ensure_source(self.definition)
            state.recover_downloading_filings(
                "Recovered after an interrupted acquisition worker"
            )
        for year in self.config.target_years:
            counts["discovered"] += await self._discover_esef_year(year)

        claimed: list[dict[str, Any]] = []
        target_year = self.config.target_years[0]
        with GlobalSourceAcquisitionState(
            self.config.database_url, ESEF_SOURCE, False
        ) as state:
            for year in self.config.target_years:
                claimed = state.claim_filings(year, self.config.esef_filing_limit)
                if claimed:
                    target_year = year
                    break

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for filing in claimed:
            queue.put_nowait(filing)
        worker_count = min(self.config.worker_count, len(claimed))
        if worker_count:
            await asyncio.gather(
                *(self._esef_worker(queue, counts) for _ in range(worker_count))
            )
        with GlobalSourceAcquisitionState(
            self.config.database_url, ESEF_SOURCE, False
        ) as state:
            stats = state.stats()
        return {
            "source_id": ESEF_SOURCE,
            "target_year": target_year,
            "status": "complete" if counts["errors"] == 0 else "partial",
            "counts": counts,
            "worker_count": worker_count,
            "effective_rps": round(self.limiter.current_rate, 3),
            "state": stats,
        }

    async def _discover_esef_year(self, year: int) -> int:
        checkpoint = f"filing-index:{year}"
        with GlobalSourceAcquisitionState(
            self.config.database_url, ESEF_SOURCE, False
        ) as state:
            if not state.checkpoint_due(
                checkpoint, self.config.discovery_refresh_hours
            ):
                return 0
            state.begin_checkpoint(checkpoint, {"year": year})
        try:
            async with await self._new_client() as client:
                filings = await self._list_esef_filings(client, year)
            entities: dict[str, EntityRef] = {}
            valid: list[FilingRef] = []
            for filing in filings:
                identifier = filing.source_entity_id
                if not identifier:
                    continue
                entities[identifier] = EntityRef(
                    entity_id=f"esef-{identifier}",
                    source_id=ESEF_SOURCE,
                    source_entity_id=identifier,
                    legal_name=str(
                        filing.metadata.get("entity_name") or identifier
                    ),
                    jurisdiction=str(
                        filing.metadata.get("country")
                        or filing.metadata.get("discovery_country")
                        or ""
                    ),
                    lei=identifier if len(identifier) == 20 else None,
                    metadata={"discovery_channel": "filings.xbrl.org"},
                )
                valid.append(filing)
            with GlobalSourceAcquisitionState(
                self.config.database_url, ESEF_SOURCE, False
            ) as state:
                state.upsert_entities(entities.values())
                count = state.upsert_filings(valid, self.config.raw_root)
                state.complete_checkpoint(
                    checkpoint,
                    {"filings_discovered": count, "entities": len(entities)},
                )
            return count
        except Exception as exc:
            with GlobalSourceAcquisitionState(
                self.config.database_url, ESEF_SOURCE, False
            ) as state:
                state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")
            raise

    async def _list_esef_filings(
        self, client: AsyncHttpClient, year: int
    ) -> list[FilingRef]:
        begin = date(year, 1, 1)
        end = date(year, 12, 31)
        output: list[FilingRef] = []
        for country in PriorityEsefAdapter.PRIORITY_COUNTRIES:
            for page in range(1, 21):
                payload = await client.get_json(
                    f"{FilingsXbrlAdapter.API_BASE}/filings",
                    params={
                        "page[size]": 200,
                        "page[number]": page,
                        "sort": "-processed",
                        "include": "entity",
                        "filter[country]": country,
                    },
                )
                rows = payload.get("data", [])
                reached_older = False
                for filing in FilingsXbrlAdapter.parse_filings(payload):
                    observed = filing.filed_at
                    if observed is None or observed > end:
                        continue
                    if observed < begin:
                        reached_older = True
                        break
                    filing.source_id = ESEF_SOURCE
                    filing.metadata["discovery_country"] = country
                    output.append(filing)
                if reached_older or not rows or not payload.get("links", {}).get("next"):
                    break
        return output

    async def _esef_worker(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        counts: dict[str, int],
    ) -> None:
        state = await asyncio.to_thread(
            GlobalSourceAcquisitionState,
            self.config.database_url,
            ESEF_SOURCE,
            False,
        )
        try:
            async with await self._new_client() as client:
                while not queue.empty():
                    try:
                        filing = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    counts["filings"] += 1
                    try:
                        document_count = await self._download_esef_filing(
                            state, client, filing
                        )
                        counts["documents"] += document_count
                    except Exception as exc:  # noqa: BLE001
                        counts["errors"] += 1
                        await asyncio.to_thread(
                            state.fail_filing,
                            filing["source_filing_id"],
                            f"{type(exc).__name__}: {exc}",
                        )
                    finally:
                        queue.task_done()
        finally:
            await asyncio.to_thread(state.close)

    async def _download_esef_filing(
        self,
        state: GlobalSourceAcquisitionState,
        client: AsyncHttpClient,
        filing: dict[str, Any],
    ) -> int:
        metadata = filing["metadata"]
        filing_id = filing["source_filing_id"]
        local_dir = Path(filing["local_dir"])
        candidates = (
            ("package", metadata.get("package_url"), "application/zip"),
            ("report", metadata.get("report_url"), "text/html"),
            ("xbrl-json", metadata.get("json_url"), "application/json"),
        )
        documents: list[dict[str, Any]] = []
        for kind, url, media_type in candidates:
            if not url:
                continue
            filename = safe_filename(str(url), f"{kind}.bin")
            result = await client.download(
                str(url),
                local_dir / filename,
                expected_media_type=media_type,
            )
            result["metadata"] = {"async": True}
            await asyncio.to_thread(
                state.upsert_document, filing_id, kind, result
            )
            documents.append(result)
        await asyncio.to_thread(
            write_manifest,
            local_dir / "filing.json",
            {"filing": filing, "documents": documents},
        )
        await asyncio.to_thread(state.complete_filing, filing_id)
        return len(documents)
