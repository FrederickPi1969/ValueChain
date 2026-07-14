from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from gcu.adapters.cvm_brazil import CvmBrazilAdapter
from gcu.config import Settings
from gcu.registry import SourceRegistry
from valuechain.acquisition_schema import AcquisitionSchemaGuard
from valuechain.async_http import AdaptiveRateLimiter, AsyncHttpClient
from valuechain.global_acquisition import (
    CVM_BRAZIL_SOURCE,
    GlobalAcquisitionConfig,
    write_manifest,
)
from valuechain.global_acquisition_state import GlobalSourceAcquisitionState
from valuechain.proxy_pool import ProxyPoolClient


REGISTRY_URL = CvmBrazilAdapter.REGISTRY_URL
DOC_BASE = CvmBrazilAdapter.DOC_BASE
FORM_INDEXES = {
    "DFP": f"{DOC_BASE}/DFP/DADOS/",
    "ITR": f"{DOC_BASE}/ITR/DADOS/",
    "FRE": f"{DOC_BASE}/FRE/DADOS/",
    "IPE": f"{DOC_BASE}/IPE/DADOS/",
}
ARCHIVE_RE = re.compile(r"(?P<form>dfp|itr|fre|ipe)_cia_aberta_(?P<year>\d{4})\.zip$")
INDEX_TAIL_RE = re.compile(
    r"(?P<modified>\d{2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2})\s+(?P<size>\S+)"
)


@dataclass(frozen=True)
class CvmBulkObject:
    form: str
    year: int
    filename: str
    url: str
    source_modified_at: datetime
    advertised_size: str

    @property
    def version(self) -> str:
        return self.source_modified_at.strftime("%Y%m%dT%H%M")

    @property
    def object_key(self) -> str:
        return f"{self.form}:{self.year}:{self.version}"

    @property
    def effective_date(self) -> date:
        return date(self.year, 12, 31)


def parse_cvm_bulk_index(
    html: str,
    *,
    form: str,
    base_url: str,
    target_years: tuple[int, ...] | None = None,
) -> list[CvmBulkObject]:
    expected_form = form.upper()
    allowed_years = set(target_years or ())
    rows: dict[str, CvmBulkObject] = {}
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        filename = Path(href.split("?", 1)[0]).name
        match = ARCHIVE_RE.fullmatch(filename)
        if not match or match.group("form").upper() != expected_form:
            continue
        year = int(match.group("year"))
        if allowed_years and year not in allowed_years:
            continue
        tail = str(anchor.next_sibling or "")
        metadata_match = INDEX_TAIL_RE.search(tail)
        if not metadata_match:
            continue
        modified = datetime.strptime(
            metadata_match.group("modified"), "%d-%b-%Y %H:%M"
        ).replace(tzinfo=UTC)
        item = CvmBulkObject(
            form=expected_form,
            year=year,
            filename=filename,
            url=urljoin(base_url, href),
            source_modified_at=modified,
            advertised_size=metadata_match.group("size"),
        )
        rows[item.object_key] = item
    return sorted(
        rows.values(),
        key=lambda row: (row.year, row.source_modified_at, row.form),
        reverse=True,
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.partial")
    with partial.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


class CvmBulkAcquisitionRunner:
    """Download versioned CVM registry and disclosure bulk archives."""

    def __init__(self, config: GlobalAcquisitionConfig) -> None:
        self.config = config
        self.settings = Settings()
        if not self.settings.proxy_pool_url:
            raise RuntimeError("VALUECHAIN_PROXY_POOL_URL is required")
        self.definition = SourceRegistry.load().get(CVM_BRAZIL_SOURCE)
        self.proxy_pool = ProxyPoolClient(self.settings.proxy_pool_url)
        self.limiter = AdaptiveRateLimiter(
            config.cvm_requests_per_second,
            minimum_requests_per_second=0.1,
        )
        self.schema_guard = AcquisitionSchemaGuard(config.database_url)

    async def _new_client(self) -> AsyncHttpClient:
        return await AsyncHttpClient.create(
            proxy_pool=self.proxy_pool,
            limiter=self.limiter,
            user_agent=self.settings.user_agent,
            contact_email=self.settings.contact_email,
            timeout_seconds=max(180.0, self.settings.http_timeout_seconds),
            max_retries=self.settings.http_max_retries,
            verify_tls=self.settings.verify_tls,
        )

    async def run_batch(self) -> dict[str, Any]:
        await self.schema_guard.prepare()
        counts = {"issuers": 0, "discovered": 0, "objects": 0, "errors": 0}
        with GlobalSourceAcquisitionState(
            self.config.database_url, CVM_BRAZIL_SOURCE, False
        ) as state:
            state.ensure_source(self.definition)
            state.recover_downloading_source_objects(
                "Recovered after an interrupted CVM bulk worker"
            )

        async with await self._new_client() as client:
            try:
                counts["issuers"] = await self._refresh_registry(client)
                counts["discovered"] = await self._discover_archives(client)
            except Exception:
                counts["errors"] += 1
                raise

            with GlobalSourceAcquisitionState(
                self.config.database_url, CVM_BRAZIL_SOURCE, False
            ) as state:
                claimed = state.claim_source_objects(self.config.cvm_bulk_object_limit)
            for row in claimed:
                try:
                    await self._download_object(client, row)
                    counts["objects"] += 1
                except Exception as exc:  # noqa: BLE001
                    counts["errors"] += 1
                    with GlobalSourceAcquisitionState(
                        self.config.database_url, CVM_BRAZIL_SOURCE, False
                    ) as state:
                        state.fail_source_object(
                            str(row["object_key"]), f"{type(exc).__name__}: {exc}"
                        )

        with GlobalSourceAcquisitionState(
            self.config.database_url, CVM_BRAZIL_SOURCE, False
        ) as state:
            return {
                "source_id": CVM_BRAZIL_SOURCE,
                "status": "complete" if counts["errors"] == 0 else "partial",
                "counts": counts,
                "effective_rps": round(self.limiter.current_rate, 3),
                "state": state.stats(),
            }

    async def _refresh_registry(self, client: AsyncHttpClient) -> int:
        checkpoint = "public-company-registry"
        with GlobalSourceAcquisitionState(
            self.config.database_url, CVM_BRAZIL_SOURCE, False
        ) as state:
            if not state.checkpoint_due(checkpoint, self.config.cvm_refresh_hours):
                return 0
            state.begin_checkpoint(checkpoint)
        try:
            response = await client.request("GET", REGISTRY_URL)
            digest = hashlib.sha256(response.content).hexdigest()
            path = (
                self.config.raw_root
                / CVM_BRAZIL_SOURCE
                / "_catalog"
                / f"cad_cia_aberta.{digest[:16]}.csv"
            )
            if not path.exists():
                _atomic_write(path, response.content)
            entities = list(
                CvmBrazilAdapter.parse_registry_file(path, active_only=True)
            )
            if not entities:
                raise ValueError("CVM registry contained no active companies")
            retrieved_at = datetime.now(UTC)
            with GlobalSourceAcquisitionState(
                self.config.database_url, CVM_BRAZIL_SOURCE, False
            ) as state:
                count = state.upsert_entities(entities)
                state.record_universe_snapshot(
                    path=path,
                    source_url=REGISTRY_URL,
                    row_count=len(entities),
                    sha256=digest,
                    retrieved_at=retrieved_at,
                )
                state.complete_checkpoint(
                    checkpoint, {"rows": len(entities), "sha256": digest}
                )
            write_manifest(
                path.with_suffix(f"{path.suffix}.manifest.json"),
                {
                    "source_id": CVM_BRAZIL_SOURCE,
                    "source_url": REGISTRY_URL,
                    "local_path": str(path),
                    "sha256": digest,
                    "row_count": len(entities),
                    "retrieved_at": retrieved_at.isoformat(),
                },
            )
            return count
        except Exception as exc:
            with GlobalSourceAcquisitionState(
                self.config.database_url, CVM_BRAZIL_SOURCE, False
            ) as state:
                state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")
            raise

    async def _discover_archives(self, client: AsyncHttpClient) -> int:
        observed = 0
        for form, index_url in FORM_INDEXES.items():
            checkpoint = f"bulk-index:{form.lower()}"
            with GlobalSourceAcquisitionState(
                self.config.database_url, CVM_BRAZIL_SOURCE, False
            ) as state:
                if not state.checkpoint_due(checkpoint, self.config.cvm_refresh_hours):
                    continue
                state.begin_checkpoint(checkpoint)
            try:
                response = await client.request("GET", index_url)
                objects = parse_cvm_bulk_index(
                    response.text,
                    form=form,
                    base_url=index_url,
                    target_years=self.config.target_years,
                )
                if not objects:
                    raise ValueError(f"CVM {form} index contained no target-year ZIPs")
                with GlobalSourceAcquisitionState(
                    self.config.database_url, CVM_BRAZIL_SOURCE, False
                ) as state:
                    for item in objects:
                        local_path = (
                            self.config.raw_root
                            / CVM_BRAZIL_SOURCE
                            / str(item.year)
                            / item.form.lower()
                            / f"{Path(item.filename).stem}.{item.version}.zip"
                        )
                        state.upsert_source_object(
                            item.object_key,
                            f"cvm-{item.form.lower()}-bulk-zip",
                            {
                                "source_url": item.url,
                                "local_path": str(local_path),
                                "content_type": "application/zip",
                                "status": "discovered",
                                "metadata": {
                                    "effective_date": item.effective_date.isoformat(),
                                    "filing_year": item.year,
                                    "form": item.form,
                                    "filename": item.filename,
                                    "source_modified_at": item.source_modified_at.isoformat(),
                                    "advertised_size": item.advertised_size,
                                    "index_url": index_url,
                                },
                            },
                        )
                    state.complete_checkpoint(
                        checkpoint,
                        {
                            "objects_observed": len(objects),
                            "latest_year": objects[0].year,
                        },
                    )
                observed += len(objects)
            except Exception as exc:
                with GlobalSourceAcquisitionState(
                    self.config.database_url, CVM_BRAZIL_SOURCE, False
                ) as state:
                    state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")
                raise
        return observed

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
                "source_id": CVM_BRAZIL_SOURCE,
                "object_key": row["object_key"],
                "object_type": row["object_type"],
                "document": result,
            },
        )
        with GlobalSourceAcquisitionState(
            self.config.database_url, CVM_BRAZIL_SOURCE, False
        ) as state:
            state.complete_source_object(str(row["object_key"]), result)
