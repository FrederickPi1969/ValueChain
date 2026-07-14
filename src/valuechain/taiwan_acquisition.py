from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gcu.adapters.twse import TpexOpenApiAdapter, TwseOpenApiAdapter
from gcu.config import Settings
from gcu.models import EntityRef, FilingRef
from gcu.registry import SourceRegistry
from valuechain.acquisition_schema import AcquisitionSchemaGuard
from valuechain.async_http import AdaptiveRateLimiter, AsyncHttpClient
from valuechain.global_acquisition import (
    TPEX_SOURCE,
    TWSE_SOURCE,
    GlobalAcquisitionConfig,
)
from valuechain.global_acquisition_state import GlobalSourceAcquisitionState
from valuechain.proxy_pool import ProxyPoolClient
from valuechain.sec_acquisition import atomic_write_json, hash_file


TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class TaiwanSourceContract:
    source_id: str
    company_url: str
    event_url: str
    financial_urls: tuple[tuple[str, str], ...]


def _financial_endpoints(base: str, market: str) -> tuple[tuple[str, str], ...]:
    suffixes = ("basi", "bd", "ci", "fh", "ins", "mim")
    balance = "t187ap07_L" if market == TWSE_SOURCE else "mopsfin_t187ap07_O"
    income = "t187ap06_L" if market == TWSE_SOURCE else "mopsfin_t187ap06_O"
    return tuple(
        (f"balance-sheet-{suffix}", f"{base}/{balance}_{suffix}")
        for suffix in suffixes
    ) + tuple(
        (f"income-statement-{suffix}", f"{base}/{income}_{suffix}")
        for suffix in suffixes
    )


SOURCE_CONTRACTS = {
    TWSE_SOURCE: TaiwanSourceContract(
        source_id=TWSE_SOURCE,
        company_url="https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        event_url="https://openapi.twse.com.tw/v1/opendata/t187ap04_L",
        financial_urls=_financial_endpoints(
            "https://openapi.twse.com.tw/v1/opendata", TWSE_SOURCE
        ),
    ),
    TPEX_SOURCE: TaiwanSourceContract(
        source_id=TPEX_SOURCE,
        company_url="https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
        event_url="https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O",
        financial_urls=_financial_endpoints(
            "https://www.tpex.org.tw/openapi/v1", TPEX_SOURCE
        ),
    ),
}


def parse_roc_date(value: Any) -> date | None:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) < 7:
        return None
    try:
        if len(digits) == 8 and digits.startswith(("19", "20")):
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        return date(int(digits[:-4]) + 1911, int(digits[-4:-2]), int(digits[-2:]))
    except ValueError:
        return None


def event_field(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    normalized = {str(key).strip(): value for key, value in row.items()}
    for name in names:
        value = normalized.get(name.strip())
        if value not in (None, ""):
            return value
    return None


def event_evidence(row: dict[str, Any]) -> str:
    subject = str(event_field(row, "主旨", "Subject") or "").strip()
    explanation = str(event_field(row, "說明", "Explanation") or "").strip()
    return "\n".join(part for part in (subject, explanation) if part)


def event_identifier(source_id: str, row: dict[str, Any]) -> str:
    stable = {str(key).strip(): value for key, value in row.items()}
    digest = hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return f"{source_id}-material-event-{digest}"


def events_to_filings(
    source_id: str, rows: list[dict[str, Any]], source_url: str
) -> list[FilingRef]:
    today = datetime.now(TAIPEI).date()
    filings: list[FilingRef] = []
    for index, row in enumerate(rows):
        ticker = str(
            event_field(row, "公司代號", "CompanyCode", "SecuritiesCompanyCode")
            or ""
        ).strip()
        if not ticker:
            continue
        filed_at = (
            parse_roc_date(event_field(row, "發言日期", "DateOfStatement", "Date"))
            or parse_roc_date(event_field(row, "事實發生日", "DateOfEvent"))
            or today
        )
        filing_id = event_identifier(source_id, row)
        filings.append(
            FilingRef(
                source_id=source_id,
                filing_id=filing_id,
                entity_id=f"{source_id}-{ticker}",
                source_entity_id=ticker,
                form="material_event",
                title=str(event_field(row, "主旨", "Subject") or "Material event"),
                filed_at=filed_at,
                detail_url=f"{source_url}#{filing_id}",
                language="zh-TW",
                metadata={
                    "channel": "official_openapi_material_event",
                    "evidence_text": event_evidence(row),
                    "raw_row_index": index,
                    "raw_record": row,
                },
            )
        )
    return filings


class TaiwanOpenApiAcquisitionRunner:
    """Ingest one Taiwan market's issuer universe, financial snapshots and events."""

    def __init__(self, source_id: str, config: GlobalAcquisitionConfig) -> None:
        if source_id not in SOURCE_CONTRACTS:
            raise ValueError(f"Unsupported Taiwan source: {source_id}")
        self.source_id = source_id
        self.contract = SOURCE_CONTRACTS[source_id]
        self.config = config
        self.settings = Settings()
        if not self.settings.proxy_pool_url:
            raise RuntimeError("VALUECHAIN_PROXY_POOL_URL is required")
        self.definition = SourceRegistry.load().get(source_id)
        self.proxy_pool = ProxyPoolClient(self.settings.proxy_pool_url)
        self.limiter = AdaptiveRateLimiter(config.taiwan_requests_per_second)
        self.schema_guard = AcquisitionSchemaGuard(config.database_url)

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

    @staticmethod
    async def _get_rows(client: AsyncHttpClient, url: str) -> list[dict[str, Any]]:
        response = await client.request("GET", url)
        payload = response.json()
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError(f"Taiwan OpenAPI returned an invalid row array: {url}")
        return payload

    async def run_batch(self) -> dict[str, Any]:
        await self.schema_guard.prepare()
        self.config.raw_root.mkdir(parents=True, exist_ok=True)
        counts = {"issuers": 0, "filings": 0, "documents": 0, "objects": 0, "errors": 0}
        with GlobalSourceAcquisitionState(
            self.config.database_url, self.source_id, False
        ) as state:
            state.ensure_source(self.definition)

        async with await self._new_client() as client:
            await self._refresh_universe(client, counts)
            await self._refresh_material_events(client, counts)
            await self._refresh_financial_snapshots(client, counts)

        with GlobalSourceAcquisitionState(
            self.config.database_url, self.source_id, False
        ) as state:
            return {
                "source_id": self.source_id,
                "status": "complete" if counts["errors"] == 0 else "partial",
                "counts": counts,
                "effective_rps": round(self.limiter.current_rate, 3),
                "state": state.stats(),
            }

    async def _refresh_universe(
        self, client: AsyncHttpClient, counts: dict[str, int]
    ) -> None:
        checkpoint = "universe:current"
        with GlobalSourceAcquisitionState(
            self.config.database_url, self.source_id, False
        ) as state:
            if not state.checkpoint_due(
                checkpoint, self.config.taiwan_snapshot_refresh_hours
            ):
                return
            state.begin_checkpoint(checkpoint)
        try:
            rows = await self._get_rows(client, self.contract.company_url)
            adapter = (
                TwseOpenApiAdapter if self.source_id == TWSE_SOURCE else TpexOpenApiAdapter
            )
            entities = list(adapter.parse_companies(rows))
            snapshot = await self._store_snapshot("company-universe", rows, self.contract.company_url)
            with GlobalSourceAcquisitionState(
                self.config.database_url, self.source_id, False
            ) as state:
                counts["issuers"] += state.upsert_entities(entities, priority=200)
                state.record_universe_snapshot(
                    path=Path(snapshot["local_path"]),
                    source_url=self.contract.company_url,
                    row_count=len(entities),
                    sha256=str(snapshot["sha256"]),
                    retrieved_at=datetime.fromisoformat(str(snapshot["retrieved_at"])),
                )
                state.upsert_source_object(
                    f"company-universe:{snapshot['sha256']}",
                    "company-universe",
                    snapshot,
                )
                state.complete_checkpoint(checkpoint, {"entities": len(entities)})
            counts["objects"] += 1
        except Exception as exc:
            counts["errors"] += 1
            with GlobalSourceAcquisitionState(
                self.config.database_url, self.source_id, False
            ) as state:
                state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")

    async def _refresh_material_events(
        self, client: AsyncHttpClient, counts: dict[str, int]
    ) -> None:
        checkpoint = "material-events:current"
        with GlobalSourceAcquisitionState(
            self.config.database_url, self.source_id, False
        ) as state:
            if not state.checkpoint_due(
                checkpoint, self.config.taiwan_event_refresh_hours
            ):
                return
            state.begin_checkpoint(checkpoint)
        try:
            rows = await self._get_rows(client, self.contract.event_url)
            snapshot = await self._store_snapshot("material-events", rows, self.contract.event_url)
            filings = events_to_filings(self.source_id, rows, self.contract.event_url)
            with GlobalSourceAcquisitionState(
                self.config.database_url, self.source_id, False
            ) as state:
                missing_entities = self._event_entities(rows, state)
                state.upsert_entities(missing_entities, priority=300)
                completed = state.complete_filing_ids(row.filing_id for row in filings)
                state.upsert_filings(filings, self.config.raw_root)
                state.upsert_source_object(
                    f"material-events:{snapshot['sha256']}",
                    "material-events-snapshot",
                    snapshot,
                )
                new_filings = [row for row in filings if row.filing_id not in completed]
                for filing in new_filings:
                    self._store_event_document(state, filing, snapshot)
                state.complete_checkpoint(
                    checkpoint,
                    {"events_observed": len(filings), "new_events": len(new_filings)},
                )
            counts["filings"] += len(new_filings)
            counts["documents"] += len(new_filings)
            counts["objects"] += 1
        except Exception as exc:
            counts["errors"] += 1
            with GlobalSourceAcquisitionState(
                self.config.database_url, self.source_id, False
            ) as state:
                state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")

    async def _refresh_financial_snapshots(
        self, client: AsyncHttpClient, counts: dict[str, int]
    ) -> None:
        checkpoint = "financial-snapshots:current"
        with GlobalSourceAcquisitionState(
            self.config.database_url, self.source_id, False
        ) as state:
            if not state.checkpoint_due(
                checkpoint, self.config.taiwan_snapshot_refresh_hours
            ):
                return
            state.begin_checkpoint(checkpoint)
        stored = 0
        try:
            for kind, url in self.contract.financial_urls:
                rows = await self._get_rows(client, url)
                snapshot = await self._store_snapshot(kind, rows, url)
                with GlobalSourceAcquisitionState(
                    self.config.database_url, self.source_id, False
                ) as state:
                    state.upsert_source_object(
                        f"{kind}:{snapshot['sha256']}", kind, snapshot
                    )
                stored += 1
            with GlobalSourceAcquisitionState(
                self.config.database_url, self.source_id, False
            ) as state:
                state.complete_checkpoint(checkpoint, {"objects": stored})
            counts["objects"] += stored
        except Exception as exc:
            counts["errors"] += 1
            with GlobalSourceAcquisitionState(
                self.config.database_url, self.source_id, False
            ) as state:
                state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")

    async def _store_snapshot(
        self, kind: str, rows: list[dict[str, Any]], source_url: str
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        payload = {
            "source_id": self.source_id,
            "kind": kind,
            "source_url": source_url,
            "retrieved_at": now.isoformat(),
            "row_count": len(rows),
            "rows": rows,
        }
        digest = hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        path = (
            self.config.raw_root
            / self.source_id
            / "snapshots"
            / now.strftime("%Y/%m/%d")
            / f"{kind}.{digest[:16]}.json"
        )
        if not path.exists():
            await asyncio.to_thread(atomic_write_json, path, payload)
        stored_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        return {
            "source_url": source_url,
            "local_path": str(path),
            "content_type": "application/json",
            "byte_size": path.stat().st_size,
            "sha256": await asyncio.to_thread(hash_file, path),
            "retrieved_at": stored_at.isoformat(),
            "status": "complete",
            "metadata": {
                "kind": kind,
                "row_count": len(rows),
                "content_sha256": digest,
            },
        }

    def _event_entities(
        self, rows: list[dict[str, Any]], state: GlobalSourceAcquisitionState
    ) -> list[EntityRef]:
        tickers = {
            str(
                event_field(
                    row, "公司代號", "CompanyCode", "SecuritiesCompanyCode"
                )
                or ""
            ).strip()
            for row in rows
        }
        tickers.discard("")
        known = state.known_issuer_ids(tickers)
        entities = []
        for row in rows:
            ticker = str(
                event_field(
                    row, "公司代號", "CompanyCode", "SecuritiesCompanyCode"
                )
                or ""
            ).strip()
            if not ticker or ticker in known:
                continue
            known.add(ticker)
            name = str(event_field(row, "公司名稱", "CompanyName") or ticker).strip()
            entities.append(
                EntityRef(
                    entity_id=f"{self.source_id}-{ticker}",
                    source_id=self.source_id,
                    source_entity_id=ticker,
                    legal_name=name,
                    jurisdiction="TW",
                    exchange="TWSE" if self.source_id == TWSE_SOURCE else "TPEx",
                    ticker=ticker,
                    metadata={"inferred_from_material_event": True},
                )
            )
        return entities

    def _store_event_document(
        self,
        state: GlobalSourceAcquisitionState,
        filing: FilingRef,
        snapshot: dict[str, Any],
    ) -> None:
        assert filing.filed_at is not None
        ticker = filing.source_entity_id or "unknown"
        path = (
            self.config.raw_root
            / self.source_id
            / "material-events"
            / filing.filed_at.strftime("%Y/%m/%d")
            / ticker
            / f"{filing.filing_id}.json"
        )
        payload = {
            "source_id": self.source_id,
            "filing_id": filing.filing_id,
            "filed_at": filing.filed_at.isoformat(),
            "source_url": filing.detail_url,
            "snapshot_path": snapshot["local_path"],
            "snapshot_sha256": snapshot["sha256"],
            "record": filing.metadata.get("raw_record", {}),
            "evidence_text": filing.metadata.get("evidence_text", ""),
        }
        atomic_write_json(path, payload)
        state.upsert_document(
            filing.filing_id,
            "material-event-json",
            {
                "source_url": filing.detail_url,
                "local_path": str(path),
                "content_type": "application/json",
                "byte_size": path.stat().st_size,
                "sha256": hash_file(path),
                "retrieved_at": snapshot["retrieved_at"],
                "status": "complete",
                "metadata": {
                    "snapshot_path": snapshot["local_path"],
                    "snapshot_sha256": snapshot["sha256"],
                },
            },
        )
        state.complete_filing(filing.filing_id)
