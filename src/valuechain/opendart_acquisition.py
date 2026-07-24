from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gcu.adapters.opendart import OpenDartAdapter
from gcu.config import Settings
from gcu.models import EntityRef, FilingRef
from gcu.registry import SourceRegistry
from valuechain.acquisition_schema import AcquisitionSchemaGuard
from valuechain.acquisition_schedule import years_with_current
from valuechain.async_http import AdaptiveRateLimiter, AsyncHttpClient
from valuechain.curated_universe import CuratedCompany, load_curated_companies
from valuechain.global_acquisition import (
    OPENDART_SOURCE,
    GlobalAcquisitionConfig,
    write_manifest,
)
from valuechain.global_acquisition_state import (
    GlobalSourceAcquisitionState,
)
from valuechain.proxy_pool import ProxyPoolClient, acquisition_uses_proxy
from valuechain.request_budget import (
    PostgresDailyRequestBudget,
    RequestBudgetExceeded,
)


EXCHANGE_BY_CLASS = {"Y": "XKRX", "K": "XKOS", "N": "XKON"}
EXCLUDED_REPORT_MARKERS = (
    "정정신고서제출요구",
)
DISCOVERY_PREFIX = "filing-index:"
UNIVERSE_CHECKPOINT = "corporation-code-universe"

def _parse_date(value: str | None) -> date | None:
    if not value or len(value) != 8 or not value.isdigit():
        return None
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


class OpenDartAcquisitionRunner:
    """Quota-guarded OpenDART discovery and original-package acquisition."""

    API_BASE = "https://opendart.fss.or.kr/api"

    def __init__(self, config: GlobalAcquisitionConfig) -> None:
        self.config = config
        self.settings = Settings()
        if acquisition_uses_proxy() and not self.settings.proxy_pool_url:
            raise RuntimeError("VALUECHAIN_PROXY_POOL_URL is required")
        if not self.settings.opendart_api_key:
            raise RuntimeError("OPENDART_API_KEY is required")
        self.definition = SourceRegistry.load().get(OPENDART_SOURCE)
        self.proxy_pool = (
            ProxyPoolClient(self.settings.proxy_pool_url)
            if acquisition_uses_proxy()
            else None
        )
        self.limiter = AdaptiveRateLimiter(config.opendart_requests_per_second)
        self.schema_guard = AcquisitionSchemaGuard(config.database_url)
        self.budget = PostgresDailyRequestBudget(
            config.database_url,
            OPENDART_SOURCE,
            config.opendart_daily_request_budget,
            timezone="Asia/Seoul",
        )
        self.watchlist = load_curated_companies("korea")
        self.watchlist_by_ticker = {company.ticker: company for company in self.watchlist}
        self._completed_discovery_dates: set[str] | None = None

    def close(self) -> None:
        self.budget.close()

    async def _reserve_request(self) -> None:
        await asyncio.to_thread(self.budget.reserve)

    async def _new_client(self) -> AsyncHttpClient:
        if self.proxy_pool is not None:
            return await AsyncHttpClient.create(
                proxy_pool=self.proxy_pool,
                limiter=self.limiter,
                user_agent=self.settings.user_agent,
                contact_email=self.settings.contact_email,
                timeout_seconds=self.settings.http_timeout_seconds,
                max_retries=self.settings.http_max_retries,
                verify_tls=self.settings.verify_tls,
                before_request=self._reserve_request,
            )
        return AsyncHttpClient(
            limiter=self.limiter,
            user_agent=self.settings.user_agent,
            contact_email=self.settings.contact_email,
            timeout_seconds=self.settings.http_timeout_seconds,
            max_retries=self.settings.http_max_retries,
            verify_tls=self.settings.verify_tls,
            before_request=self._reserve_request,
        )

    async def run_batch(self) -> dict[str, Any]:
        await self.schema_guard.prepare()
        counts = {
            "discovered": 0,
            "filings": 0,
            "documents": 0,
            "errors": 0,
        }
        snapshot = await asyncio.to_thread(self.budget.snapshot)
        if snapshot.remaining == 0:
            return self._result("budget_exhausted", counts, snapshot)
        try:
            return await self._run_batch(counts)
        except RequestBudgetExceeded:
            snapshot = await asyncio.to_thread(self.budget.snapshot)
            return self._result("budget_exhausted", counts, snapshot)

    async def _run_batch(self, counts: dict[str, int]) -> dict[str, Any]:
        self.config.raw_root.mkdir(parents=True, exist_ok=True)
        with GlobalSourceAcquisitionState(
            self.config.database_url, OPENDART_SOURCE, False
        ) as state:
            state.ensure_source(self.definition)
            state.recover_downloading_filings(
                "Recovered after an interrupted OpenDART acquisition worker"
            )
            if self._completed_discovery_dates is None:
                self._completed_discovery_dates = state.completed_checkpoint_keys(
                    DISCOVERY_PREFIX
                )

        await self._refresh_universe_if_due()
        discovery_date = self._next_discovery_date()
        if discovery_date is not None:
            counts["discovered"] += await self._discover_date(discovery_date)

        snapshot = await asyncio.to_thread(self.budget.snapshot)
        worst_case_attempts = self.settings.http_max_retries + 1
        claim_limit = min(
            self.config.opendart_filing_limit,
            snapshot.remaining // worst_case_attempts,
        )
        claimed: list[dict[str, Any]] = []
        target_year = datetime.now(UTC).year
        if claim_limit:
            years = years_with_current(self.config.target_years, target_year)
            with GlobalSourceAcquisitionState(
                self.config.database_url, OPENDART_SOURCE, False
            ) as state:
                for statuses in (("discovered",), ("retry",)):
                    for year in years:
                        claimed = state.claim_filings(year, claim_limit, statuses=statuses)
                        if claimed:
                            target_year = year
                            break
                    if claimed:
                        break

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for filing in claimed:
            queue.put_nowait(filing)
        worker_count = min(self.config.opendart_worker_count, len(claimed))
        if worker_count:
            await asyncio.gather(
                *(self._worker(queue, counts) for _ in range(worker_count))
            )
        snapshot = await asyncio.to_thread(self.budget.snapshot)
        status = "complete" if counts["errors"] == 0 else "partial"
        result = self._result(status, counts, snapshot)
        result.update(
            {
                "target_year": target_year,
                "discovery_date": discovery_date,
                "worker_count": worker_count,
                "effective_rps": round(self.limiter.current_rate, 3),
                "watchlist_companies": len(self.watchlist),
            }
        )
        with GlobalSourceAcquisitionState(
            self.config.database_url, OPENDART_SOURCE, False
        ) as state:
            result["state"] = state.stats()
        return result

    def _result(
        self, status: str, counts: dict[str, int], snapshot: Any
    ) -> dict[str, Any]:
        return {
            "source_id": OPENDART_SOURCE,
            "status": status,
            "counts": counts,
            "request_budget": {
                "date": snapshot.usage_date,
                "used": snapshot.used,
                "limit": snapshot.limit,
                "remaining": snapshot.remaining,
                "timezone": "Asia/Seoul",
            },
        }

    async def _refresh_universe_if_due(self) -> int:
        with GlobalSourceAcquisitionState(
            self.config.database_url, OPENDART_SOURCE, False
        ) as state:
            if not state.checkpoint_due(
                UNIVERSE_CHECKPOINT, self.config.opendart_universe_refresh_hours
            ):
                return 0
            state.begin_checkpoint(UNIVERSE_CHECKPOINT)
        try:
            now = datetime.now(UTC)
            path = (
                self.config.raw_root
                / OPENDART_SOURCE
                / "_catalog"
                / f"corpCode.{now.strftime('%Y%m%dT%H%M%SZ')}.zip"
            )
            async with await self._new_client() as client:
                result = await client.download(
                    f"{self.API_BASE}/corpCode.xml",
                    path,
                    expected_media_type="application/zip",
                    params={"crtfc_key": self.settings.opendart_api_key},
                )
            content = await asyncio.to_thread(path.read_bytes)
            if not content.startswith(b"PK"):
                raise ValueError(
                    "OpenDART corporation-code response was not a ZIP archive"
                )
            records = list(OpenDartAdapter.parse_corp_code_zip(content))
            matched_tickers: set[str] = set()
            entities = [
                EntityRef(
                    entity_id=f"opendart-{row['corp_code']}",
                    source_id=OPENDART_SOURCE,
                    source_entity_id=row["corp_code"],
                    legal_name=(
                        row.get("corp_eng_name")
                        or row.get("corp_name")
                        or row["corp_code"]
                    ),
                    jurisdiction="KR",
                    exchange=EXCHANGE_BY_CLASS.get(str(row.get("corp_cls") or ""), "XKRX"),
                    ticker=row.get("stock_code") or None,
                    local_registry_id=row["corp_code"],
                    aliases=[row["corp_name"]] if row.get("corp_name") else [],
                    metadata={
                        **row,
                        **self.watchlist_by_ticker[str(row.get("stock_code"))].metadata(),
                    },
                )
                for row in records
                if str(row.get("stock_code") or "") in self.watchlist_by_ticker
            ]
            matched_tickers.update(entity.ticker or "" for entity in entities)
            missing = sorted(set(self.watchlist_by_ticker) - matched_tickers)
            minimum_matches = max(1, int(len(self.watchlist) * 0.85))
            if len(entities) < minimum_matches:
                raise ValueError(
                    f"OpenDART watchlist match coverage too low: {len(entities)}/"
                    f"{len(self.watchlist)}; missing={missing[:10]}"
                )
            with GlobalSourceAcquisitionState(
                self.config.database_url, OPENDART_SOURCE, False
            ) as state:
                count = state.upsert_entities(entities, priority=100)
                state.record_universe_snapshot(
                    path=path,
                    source_url=f"{self.API_BASE}/corpCode.xml",
                    row_count=count,
                    sha256=str(result["sha256"]),
                    retrieved_at=now,
                )
                state.complete_checkpoint(
                    UNIVERSE_CHECKPOINT,
                    {
                        "watchlist_entities": count,
                        "watchlist_size": len(self.watchlist),
                        "all_corporations": len(records),
                        "missing_tickers": missing,
                    },
                )
            return count
        except Exception as exc:
            with GlobalSourceAcquisitionState(
                self.config.database_url, OPENDART_SOURCE, False
            ) as state:
                state.fail_checkpoint(
                    UNIVERSE_CHECKPOINT, f"{type(exc).__name__}: {exc}"
                )
            raise

    def _next_discovery_date(self) -> date | None:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        completed = self._completed_discovery_dates or set()
        with GlobalSourceAcquisitionState(
            self.config.database_url, OPENDART_SOURCE, False
        ) as state:
            for offset in range(self.config.opendart_discovery_lookback_days):
                candidate = today - timedelta(days=offset)
                key = f"{DISCOVERY_PREFIX}{candidate.isoformat()}"
                if state.checkpoint_due(
                    key, self.config.opendart_discovery_refresh_hours
                ):
                    return candidate
            for year in years_with_current(self.config.target_years, today.year):
                start = date(year, 1, 1)
                end = min(today, date(year, 12, 31))
                if end < start:
                    continue
                candidate = end
                while candidate >= start:
                    key = f"{DISCOVERY_PREFIX}{candidate.isoformat()}"
                    if key not in completed and state.checkpoint_due(key, 24 * 3650):
                        return candidate
                    candidate -= timedelta(days=1)
        return None

    async def _discover_date(self, filing_date: date) -> int:
        checkpoint = f"{DISCOVERY_PREFIX}{filing_date.isoformat()}"
        with GlobalSourceAcquisitionState(
            self.config.database_url, OPENDART_SOURCE, False
        ) as state:
            state.begin_checkpoint(checkpoint, {"date": filing_date.isoformat()})
        try:
            async with await self._new_client() as client:
                records, pages = await self._list_records(client, filing_date)
            with GlobalSourceAcquisitionState(
                self.config.database_url, OPENDART_SOURCE, False
            ) as state:
                known = state.issuer_ids_for_tickers(self.watchlist_by_ticker)
                selected = [
                    row
                    for row in records
                    if str(row.get("corp_code") or "") in known
                    and not self._is_non_downloadable_notice(row)
                ]
                entities, filings = self._convert_records(selected)
                entities = [self._enrich_watchlist_entity(entity) for entity in entities]
                state.upsert_entities(entities, priority=100)
                count = state.upsert_filings(filings, self.config.raw_root)
                state.complete_checkpoint(
                    checkpoint,
                    {
                        "pages": pages,
                        "records_observed": len(records),
                        "watchlist_filings": count,
                        "watchlist_size": len(self.watchlist),
                    },
                )
            if self._completed_discovery_dates is not None:
                self._completed_discovery_dates.add(checkpoint)
            return count
        except Exception as exc:
            with GlobalSourceAcquisitionState(
                self.config.database_url, OPENDART_SOURCE, False
            ) as state:
                state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")
            raise

    @staticmethod
    def _is_non_downloadable_notice(record: dict[str, Any]) -> bool:
        report_name = "".join(str(record.get("report_nm") or "").split())
        return any(marker in report_name for marker in EXCLUDED_REPORT_MARKERS)

    def _enrich_watchlist_entity(self, entity: EntityRef) -> EntityRef:
        company: CuratedCompany | None = self.watchlist_by_ticker.get(entity.ticker or "")
        if company is None:
            return entity
        return entity.model_copy(
            update={"metadata": {**entity.metadata, **company.metadata()}}
        )

    async def _list_records(
        self, client: AsyncHttpClient, filing_date: date
    ) -> tuple[list[dict[str, Any]], int]:
        output: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            payload = await client.get_json(
                f"{self.API_BASE}/list.json",
                params={
                    "crtfc_key": self.settings.opendart_api_key,
                    "bgn_de": filing_date.strftime("%Y%m%d"),
                    "end_de": filing_date.strftime("%Y%m%d"),
                    "page_no": page,
                    "page_count": 100,
                    "sort": "date",
                    "sort_mth": "desc",
                },
            )
            status = str(payload.get("status") or "")
            if status == "013":
                return output, page
            if status == "020":
                await asyncio.to_thread(self.budget.exhaust)
                raise RequestBudgetExceeded("OpenDART returned call-limit status 020")
            if status != "000":
                raise ValueError(
                    f"OpenDART list error {status}: {payload.get('message')}"
                )
            output.extend(payload.get("list") or [])
            total_pages = max(1, int(payload.get("total_page") or 1))
            page += 1
        return output, total_pages

    @classmethod
    def _convert_records(
        cls, records: list[dict[str, Any]]
    ) -> tuple[list[EntityRef], list[FilingRef]]:
        entities: dict[str, EntityRef] = {}
        filings: list[FilingRef] = []
        for row in records:
            corp_code = str(row.get("corp_code") or "")
            receipt = str(row.get("rcept_no") or "")
            filed_at = _parse_date(str(row.get("rcept_dt") or ""))
            if not corp_code or not receipt or filed_at is None:
                continue
            corp_class = str(row.get("corp_cls") or "")
            entities[corp_code] = EntityRef(
                entity_id=f"opendart-{corp_code}",
                source_id=OPENDART_SOURCE,
                source_entity_id=corp_code,
                legal_name=str(row.get("corp_name") or corp_code),
                jurisdiction="KR",
                exchange=EXCHANGE_BY_CLASS.get(corp_class, "XKRX"),
                ticker=str(row.get("stock_code") or "") or None,
                local_registry_id=corp_code,
                metadata={"corp_cls": corp_class},
            )
            report_name = str(row.get("report_nm") or "unknown")
            document_url = f"{cls.API_BASE}/document.xml?rcept_no={receipt}"
            filings.append(
                FilingRef(
                    source_id=OPENDART_SOURCE,
                    filing_id=receipt,
                    entity_id=f"opendart-{corp_code}",
                    source_entity_id=corp_code,
                    form=report_name,
                    title=report_name,
                    filed_at=filed_at,
                    detail_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}",
                    primary_document_url=document_url,
                    language="ko",
                    amendment="정정" in report_name,
                    metadata={**row, "discovery_channel": "opendart_global_daily"},
                )
            )
        return list(entities.values()), filings

    async def _worker(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        counts: dict[str, int],
    ) -> None:
        state = await asyncio.to_thread(
            GlobalSourceAcquisitionState,
            self.config.database_url,
            OPENDART_SOURCE,
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
                        counts["documents"] += await self._download_filing(
                            state, client, filing
                        )
                    except Exception as exc:  # noqa: BLE001
                        counts["errors"] += 1
                        await asyncio.to_thread(
                            state.fail_filing,
                            filing["source_filing_id"],
                            f"{type(exc).__name__}: {exc}",
                            60,
                        )
                    finally:
                        queue.task_done()
        finally:
            await asyncio.to_thread(state.close)

    async def _download_filing(
        self,
        state: GlobalSourceAcquisitionState,
        client: AsyncHttpClient,
        filing: dict[str, Any],
    ) -> int:
        filing_id = str(filing["source_filing_id"])
        local_dir = Path(filing["local_dir"])
        source_url = str(
            filing["metadata"].get("primary_document_url")
            or f"{self.API_BASE}/document.xml?rcept_no={filing_id}"
        )
        output_path = local_dir / f"{filing_id}.zip"
        result = await client.download(
            source_url,
            output_path,
            expected_media_type="application/zip",
            params={"crtfc_key": self.settings.opendart_api_key},
        )
        result["metadata"] = {
            "async": True,
            "quota_guarded": True,
            "package": "original_disclosure",
        }
        await asyncio.to_thread(
            state.upsert_document, filing_id, "original-disclosure-package", result
        )
        await asyncio.to_thread(
            write_manifest,
            local_dir / "filing.json",
            {"filing": filing, "documents": [result]},
        )
        await asyncio.to_thread(state.complete_filing, filing_id)
        return 1
