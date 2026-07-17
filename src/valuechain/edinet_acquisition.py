from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gcu.config import Settings
from gcu.models import EntityRef, FilingRef
from gcu.registry import SourceRegistry
from valuechain.acquisition_schema import AcquisitionSchemaGuard
from valuechain.acquisition_schedule import years_with_current
from valuechain.async_http import AdaptiveRateLimiter, AsyncHttpClient
from valuechain.curated_universe import CuratedCompany, load_curated_companies
from valuechain.global_acquisition import (
    EDINET_SOURCE,
    GlobalAcquisitionConfig,
    write_manifest,
)
from valuechain.global_acquisition_state import GlobalSourceAcquisitionState
from valuechain.proxy_pool import ProxyPoolClient, acquisition_uses_proxy
from valuechain.request_budget import PostgresDailyRequestBudget, RequestBudgetExceeded


DISCOVERY_PREFIX = "filing-index:"

# Issuer disclosures useful for financial and dependency analysis. Confirmation,
# internal-control, fund and large-holder reports are intentionally excluded.
EDINET_DOCUMENT_TYPE_CODES = {
    "030",  # securities registration statement
    "040",  # amended securities registration statement
    "060",  # shelf registration statement
    "070",  # amended shelf registration statement
    "080",  # shelf registration supplement
    "090",  # amended shelf registration document
    "120",  # annual securities report
    "130",  # amended annual securities report
    "140",  # quarterly report
    "150",  # amended quarterly report
    "160",  # semiannual report
    "170",  # amended semiannual report
    "180",  # extraordinary report
    "190",  # amended extraordinary report
}


def normalize_edinet_ticker(value: Any) -> str:
    code = str(value or "").strip()
    if len(code) == 5 and code.endswith("0") and code.isdigit():
        return code[:4]
    return code


def parse_edinet_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


class EdinetAcquisitionRunner:
    """Curated, quota-guarded EDINET document acquisition."""

    API_BASE = "https://api.edinet-fsa.go.jp/api/v2"

    def __init__(self, config: GlobalAcquisitionConfig) -> None:
        self.config = config
        self.settings = Settings()
        if acquisition_uses_proxy() and not self.settings.proxy_pool_url:
            raise RuntimeError("VALUECHAIN_PROXY_POOL_URL is required")
        if not self.settings.edinet_api_key:
            raise RuntimeError("EDINET_API_KEY is required")
        self.definition = SourceRegistry.load().get(EDINET_SOURCE)
        self.proxy_pool = (
            ProxyPoolClient(self.settings.proxy_pool_url)
            if acquisition_uses_proxy()
            else None
        )
        self.limiter = AdaptiveRateLimiter(config.edinet_requests_per_second)
        self.schema_guard = AcquisitionSchemaGuard(config.database_url)
        self.budget = PostgresDailyRequestBudget(
            config.database_url,
            EDINET_SOURCE,
            config.edinet_daily_request_budget,
            timezone="Asia/Tokyo",
        )
        self.watchlist = load_curated_companies("japan")
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
        counts = {"discovered": 0, "filings": 0, "documents": 0, "errors": 0}
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
            self.config.database_url, EDINET_SOURCE, False
        ) as state:
            state.ensure_source(self.definition)
            state.recover_downloading_filings(
                "Recovered after an interrupted EDINET acquisition worker"
            )
            if self._completed_discovery_dates is None:
                self._completed_discovery_dates = state.completed_checkpoint_keys(
                    DISCOVERY_PREFIX
                )

        discovery_date = self._next_discovery_date()
        if discovery_date is not None:
            counts["discovered"] += await self._discover_date(discovery_date)

        snapshot = await asyncio.to_thread(self.budget.snapshot)
        worst_case_attempts = self.settings.http_max_retries + 1
        claim_limit = min(
            self.config.edinet_filing_limit,
            snapshot.remaining // worst_case_attempts,
        )
        claimed: list[dict[str, Any]] = []
        target_year = datetime.now(UTC).year
        if claim_limit:
            years = years_with_current(self.config.target_years, target_year)
            with GlobalSourceAcquisitionState(
                self.config.database_url, EDINET_SOURCE, False
            ) as state:
                for year in years:
                    claimed = state.claim_filings(year, claim_limit)
                    if claimed:
                        target_year = year
                        break

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for filing in claimed:
            queue.put_nowait(filing)
        worker_count = min(self.config.edinet_worker_count, len(claimed))
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
            self.config.database_url, EDINET_SOURCE, False
        ) as state:
            result["state"] = state.stats()
        return result

    @staticmethod
    def _result(status: str, counts: dict[str, int], snapshot: Any) -> dict[str, Any]:
        return {
            "source_id": EDINET_SOURCE,
            "status": status,
            "counts": counts,
            "request_budget": {
                "date": snapshot.usage_date,
                "used": snapshot.used,
                "limit": snapshot.limit,
                "remaining": snapshot.remaining,
                "timezone": "Asia/Tokyo",
            },
        }

    def _next_discovery_date(self) -> date | None:
        today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
        completed = self._completed_discovery_dates or set()
        with GlobalSourceAcquisitionState(
            self.config.database_url, EDINET_SOURCE, False
        ) as state:
            for offset in range(self.config.edinet_discovery_lookback_days):
                candidate = today - timedelta(days=offset)
                key = f"{DISCOVERY_PREFIX}{candidate.isoformat()}"
                if state.checkpoint_due(key, self.config.edinet_discovery_refresh_hours):
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
            self.config.database_url, EDINET_SOURCE, False
        ) as state:
            state.begin_checkpoint(checkpoint, {"date": filing_date.isoformat()})
        try:
            async with await self._new_client() as client:
                payload = await client.get_json(
                    f"{self.API_BASE}/documents.json",
                    params={
                        "date": filing_date.isoformat(),
                        "type": 2,
                        "Subscription-Key": self.settings.edinet_api_key,
                    },
                )
            metadata_status = str((payload.get("metadata") or {}).get("status") or "")
            if metadata_status != "200":
                raise ValueError(f"EDINET list metadata status {metadata_status or 'missing'}")
            records = list(payload.get("results") or [])
            selected = self._select_records(records, self.watchlist_by_ticker)
            entities, filings = self._convert_records(selected, self.watchlist_by_ticker)
            with GlobalSourceAcquisitionState(
                self.config.database_url, EDINET_SOURCE, False
            ) as state:
                state.upsert_entities(entities, priority=100)
                count = state.upsert_filings(filings, self.config.raw_root)
                state.complete_checkpoint(
                    checkpoint,
                    {
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
                self.config.database_url, EDINET_SOURCE, False
            ) as state:
                state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")
            raise

    @staticmethod
    def _select_records(
        records: list[dict[str, Any]],
        watchlist_by_ticker: dict[str, CuratedCompany],
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in records
            if normalize_edinet_ticker(row.get("secCode")) in watchlist_by_ticker
            and str(row.get("docTypeCode") or "") in EDINET_DOCUMENT_TYPE_CODES
            and str(row.get("withdrawalStatus") or "0") != "1"
        ]

    @classmethod
    def _convert_records(
        cls,
        records: list[dict[str, Any]],
        watchlist_by_ticker: dict[str, CuratedCompany],
    ) -> tuple[list[EntityRef], list[FilingRef]]:
        entities: dict[str, EntityRef] = {}
        filings: list[FilingRef] = []
        for row in records:
            document_id = str(row.get("docID") or "")
            edinet_code = str(row.get("edinetCode") or "")
            ticker = normalize_edinet_ticker(row.get("secCode"))
            filed_at = parse_edinet_date(row.get("submitDateTime"))
            company = watchlist_by_ticker.get(ticker)
            if not document_id or not edinet_code or filed_at is None or company is None:
                continue
            metadata = {**row, **company.metadata()}
            entities[edinet_code] = EntityRef(
                entity_id=f"edinet-{edinet_code}",
                source_id=EDINET_SOURCE,
                source_entity_id=edinet_code,
                legal_name=str(row.get("filerName") or company.company_name),
                jurisdiction="JP",
                exchange="XTKS",
                ticker=ticker,
                local_registry_id=str(row.get("JCN") or "") or None,
                aliases=[company.company_name],
                metadata=metadata,
            )
            title = str(row.get("docDescription") or row.get("filerName") or document_id)
            filings.append(
                FilingRef(
                    source_id=EDINET_SOURCE,
                    filing_id=document_id,
                    entity_id=f"edinet-{edinet_code}",
                    source_entity_id=edinet_code,
                    form=str(row.get("docTypeCode") or "unknown"),
                    title=title,
                    filed_at=filed_at,
                    period_end=parse_edinet_date(row.get("periodEnd")),
                    detail_url=(
                        "https://disclosure2.edinet-fsa.go.jp/"
                        f"WEEE0030.aspx?docID={document_id}"
                    ),
                    primary_document_url=f"{cls.API_BASE}/documents/{document_id}",
                    language="ja",
                    amendment=str(row.get("docTypeCode") or "")
                    in {"040", "070", "090", "130", "150", "170", "190"},
                    metadata={**metadata, "discovery_channel": "edinet_curated_daily"},
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
            EDINET_SOURCE,
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
            or f"{self.API_BASE}/documents/{filing_id}"
        )
        output_path = local_dir / f"{filing_id}.xbrl.zip"
        result = await client.download(
            source_url,
            output_path,
            expected_media_type="application/zip",
            params={"type": 1, "Subscription-Key": self.settings.edinet_api_key},
        )
        result["metadata"] = {
            "async": True,
            "quota_guarded": True,
            "package": "xbrl_submission",
            "edinet_output_type": 1,
        }
        await asyncio.to_thread(
            state.upsert_document, filing_id, "xbrl-submission-package", result
        )
        await asyncio.to_thread(
            write_manifest,
            local_dir / "filing.json",
            {"filing": filing, "documents": [result]},
        )
        await asyncio.to_thread(state.complete_filing, filing_id)
        return 1
