from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from gcu.config import Settings
from gcu.registry import SourceRegistry
from valuechain.acquisition_schema import AcquisitionSchemaGuard
from valuechain.async_http import AdaptiveRateLimiter, AsyncHttpClient
from valuechain.global_acquisition import (
    COMPANIES_HOUSE_BULK_SOURCE,
    GlobalAcquisitionConfig,
    write_manifest,
)
from valuechain.global_acquisition_state import GlobalSourceAcquisitionState
from valuechain.proxy_pool import ProxyPoolClient


INDEX_URL = "https://download.companieshouse.gov.uk/en_accountsdata.html"
DAILY_FILE_RE = re.compile(r"Accounts_Bulk_Data-(\d{4}-\d{2}-\d{2})\.zip$")


@dataclass(frozen=True)
class AccountsBulkObject:
    effective_date: date
    filename: str
    url: str

    @property
    def object_key(self) -> str:
        return f"daily-accounts:{self.effective_date.isoformat()}"


def parse_accounts_bulk_index(html: str, base_url: str = INDEX_URL) -> list[AccountsBulkObject]:
    rows: dict[str, AccountsBulkObject] = {}
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        filename = Path(href.split("?", 1)[0]).name
        match = DAILY_FILE_RE.fullmatch(filename)
        if not match:
            continue
        effective_date = date.fromisoformat(match.group(1))
        rows[filename] = AccountsBulkObject(
            effective_date=effective_date,
            filename=filename,
            url=urljoin(base_url, href),
        )
    return sorted(rows.values(), key=lambda row: row.effective_date, reverse=True)


class CompaniesHouseBulkAcquisitionRunner:
    """Discover and download public Companies House daily accounts XBRL ZIPs."""

    def __init__(self, config: GlobalAcquisitionConfig) -> None:
        self.config = config
        self.settings = Settings()
        if not self.settings.proxy_pool_url:
            raise RuntimeError("VALUECHAIN_PROXY_POOL_URL is required")
        self.definition = SourceRegistry.load().get(COMPANIES_HOUSE_BULK_SOURCE)
        self.proxy_pool = ProxyPoolClient(self.settings.proxy_pool_url)
        self.limiter = AdaptiveRateLimiter(
            config.companies_house_bulk_requests_per_second,
            minimum_requests_per_second=0.1,
        )
        self.schema_guard = AcquisitionSchemaGuard(config.database_url)

    async def _new_client(self) -> AsyncHttpClient:
        return await AsyncHttpClient.create(
            proxy_pool=self.proxy_pool,
            limiter=self.limiter,
            user_agent=self.settings.user_agent,
            contact_email=self.settings.contact_email,
            timeout_seconds=max(120.0, self.settings.http_timeout_seconds),
            max_retries=self.settings.http_max_retries,
            verify_tls=self.settings.verify_tls,
        )

    async def run_batch(self) -> dict[str, Any]:
        await self.schema_guard.prepare()
        self.config.raw_root.mkdir(parents=True, exist_ok=True)
        counts = {"discovered": 0, "objects": 0, "errors": 0}
        with GlobalSourceAcquisitionState(
            self.config.database_url, COMPANIES_HOUSE_BULK_SOURCE, False
        ) as state:
            state.ensure_source(self.definition)
            state.recover_downloading_source_objects(
                "Recovered after an interrupted Companies House bulk worker"
            )

        async with await self._new_client() as client:
            counts["discovered"] = await self._discover(client)
            with GlobalSourceAcquisitionState(
                self.config.database_url, COMPANIES_HOUSE_BULK_SOURCE, False
            ) as state:
                claimed = state.claim_source_objects(
                    self.config.companies_house_bulk_object_limit
                )
            for row in claimed:
                try:
                    await self._download_object(client, row)
                    counts["objects"] += 1
                except Exception as exc:
                    counts["errors"] += 1
                    with GlobalSourceAcquisitionState(
                        self.config.database_url,
                        COMPANIES_HOUSE_BULK_SOURCE,
                        False,
                    ) as state:
                        state.fail_source_object(
                            str(row["object_key"]), f"{type(exc).__name__}: {exc}"
                        )

        with GlobalSourceAcquisitionState(
            self.config.database_url, COMPANIES_HOUSE_BULK_SOURCE, False
        ) as state:
            return {
                "source_id": COMPANIES_HOUSE_BULK_SOURCE,
                "status": "complete" if counts["errors"] == 0 else "partial",
                "counts": counts,
                "effective_rps": round(self.limiter.current_rate, 3),
                "state": state.stats(),
            }

    async def _discover(self, client: AsyncHttpClient) -> int:
        checkpoint = "daily-accounts-index"
        with GlobalSourceAcquisitionState(
            self.config.database_url, COMPANIES_HOUSE_BULK_SOURCE, False
        ) as state:
            if not state.checkpoint_due(
                checkpoint, self.config.companies_house_bulk_refresh_hours
            ):
                return 0
            state.begin_checkpoint(checkpoint)
        try:
            response = await client.request("GET", INDEX_URL)
            objects = parse_accounts_bulk_index(response.text)
            if not objects:
                raise ValueError("Companies House daily accounts index contained no ZIPs")
            with GlobalSourceAcquisitionState(
                self.config.database_url, COMPANIES_HOUSE_BULK_SOURCE, False
            ) as state:
                for item in objects:
                    local_path = (
                        self.config.raw_root
                        / COMPANIES_HOUSE_BULK_SOURCE
                        / item.effective_date.strftime("%Y/%m")
                        / item.filename
                    )
                    state.upsert_source_object(
                        item.object_key,
                        "daily-accounts-xbrl-zip",
                        {
                            "source_url": item.url,
                            "local_path": str(local_path),
                            "content_type": "application/zip",
                            "status": "discovered",
                            "metadata": {
                                "effective_date": item.effective_date.isoformat(),
                                "filename": item.filename,
                                "index_url": INDEX_URL,
                            },
                        },
                    )
                state.complete_checkpoint(
                    checkpoint,
                    {
                        "objects_observed": len(objects),
                        "latest_date": objects[0].effective_date.isoformat(),
                    },
                )
            return len(objects)
        except Exception as exc:
            with GlobalSourceAcquisitionState(
                self.config.database_url, COMPANIES_HOUSE_BULK_SOURCE, False
            ) as state:
                state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")
            raise

    async def _download_object(
        self, client: AsyncHttpClient, row: dict[str, Any]
    ) -> None:
        path = Path(str(row["local_path"]))
        result = await client.download(
            str(row["source_url"]), path, expected_media_type="application/zip"
        )
        result["metadata"] = {
            **dict(row.get("metadata") or {}),
            "cached": result.get("cached", False),
            "resumed_from": result.get("resumed_from", 0),
            "final_url": result.get("final_url", ""),
        }
        write_manifest(
            path.with_suffix(f"{path.suffix}.manifest.json"),
            {
                "source_id": COMPANIES_HOUSE_BULK_SOURCE,
                "object_key": row["object_key"],
                "object_type": row["object_type"],
                "document": result,
            },
        )
        with GlobalSourceAcquisitionState(
            self.config.database_url, COMPANIES_HOUSE_BULK_SOURCE, False
        ) as state:
            state.complete_source_object(str(row["object_key"]), result)
