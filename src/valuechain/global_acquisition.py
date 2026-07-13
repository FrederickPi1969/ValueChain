from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gcu.config import Settings
from gcu.gleif_bulk import GleifGoldenCopy
from gcu.http import PoliteHttpClient
from gcu.models import DocumentRef, EntityRef, FilingRef
from gcu.registry import SourceRegistry
from gcu_priority_markets.adapters.cninfo import CninfoAdapter
from gcu_priority_markets.registry import PatchRegistry
from valuechain.acquisition_state import AcquisitionIssuer
from valuechain.global_acquisition_state import GlobalSourceAcquisitionState
from valuechain.postgres_acquisition_state import PostgresAcquisitionState
from valuechain.sec_acquisition import hash_file, parse_target_years


CNINFO_SOURCE = "cninfo"
ESEF_SOURCE = "priority_eu_esef"
GLEIF_SOURCE = "gleif_golden_copy"
SUPPORTED_SOURCES = (CNINFO_SOURCE, ESEF_SOURCE, GLEIF_SOURCE)


def require_proxy(settings: Settings) -> Settings:
    """Fail closed so scheduled acquisition never silently uses a direct connection."""
    if not settings.proxy_pool_url:
        raise RuntimeError(
            "VALUECHAIN_PROXY_POOL_URL is required for global acquisition"
        )
    return settings


@dataclass(frozen=True)
class GlobalAcquisitionConfig:
    raw_root: Path
    database_url: str
    target_years: tuple[int, ...] = (2026, 2025)
    cninfo_issuer_limit: int = 16
    esef_filing_limit: int = 16
    worker_count: int = 4
    cninfo_requests_per_second: float = 2.0
    esef_requests_per_second: float = 4.0
    cninfo_rescan_hours: int = 24
    discovery_refresh_hours: int = 24

    @classmethod
    def from_env(cls) -> GlobalAcquisitionConfig:
        return cls(
            raw_root=Path(
                os.getenv(
                    "VALUECHAIN_GLOBAL_RAW_DIR",
                    "/mnt/hdd8tb/valuechain/global-acquisition",
                )
            ).expanduser(),
            database_url=os.getenv(
                "VALUECHAIN_ACQUISITION_DATABASE_URL",
                os.getenv(
                    "VALUECHAIN_DATABASE_URL",
                    "postgresql://valuechain:valuechain_dev@127.0.0.1:5433/valuechain",
                ),
            ),
            target_years=parse_target_years(
                os.getenv("VALUECHAIN_GLOBAL_ACQUISITION_YEARS", "2026,2025")
            ),
            cninfo_issuer_limit=max(
                1, int(os.getenv("VALUECHAIN_CNINFO_ISSUER_LIMIT", "16"))
            ),
            esef_filing_limit=max(
                1, int(os.getenv("VALUECHAIN_ESEF_FILING_LIMIT", "16"))
            ),
            worker_count=min(
                4,
                max(1, int(os.getenv("VALUECHAIN_GLOBAL_CONCURRENCY", "4"))),
            ),
            cninfo_requests_per_second=max(
                0.25,
                float(os.getenv("VALUECHAIN_CNINFO_REQUESTS_PER_SECOND", "2.0")),
            ),
            esef_requests_per_second=max(
                0.25,
                float(os.getenv("VALUECHAIN_ESEF_REQUESTS_PER_SECOND", "4.0")),
            ),
            cninfo_rescan_hours=max(
                1, int(os.getenv("VALUECHAIN_CNINFO_RESCAN_HOURS", "24"))
            ),
            discovery_refresh_hours=max(
                1, int(os.getenv("VALUECHAIN_GLOBAL_DISCOVERY_REFRESH_HOURS", "24"))
            ),
        )


def safe_filename(value: str, fallback: str) -> str:
    name = Path(urlparse(value).path).name or fallback
    cleaned = "".join(character if character.isalnum() or character in ".-_" else "_" for character in name)
    return cleaned[:240] or fallback


def download_document(
    client: PoliteHttpClient,
    document: DocumentRef,
    output_path: Path,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and output_path.stat().st_size > 0:
        return {
            "source_url": document.url,
            "local_path": str(output_path),
            "content_type": document.expected_media_type or "",
            "byte_size": output_path.stat().st_size,
            "sha256": hash_file(output_path),
            "retrieved_at": datetime.fromtimestamp(output_path.stat().st_mtime, UTC).isoformat(),
            "status": "complete",
            "metadata": {"cached": True, **document.metadata},
        }
    partial = output_path.with_name(f"{output_path.name}.partial")
    with client.stream_to_temporary_file(document.url) as payload:
        client.validate_payload(payload, document.expected_media_type, output_path.name)
        with payload.temporary_path.open("rb") as source, partial.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.replace(partial, output_path)
        return {
            "source_url": document.url,
            "local_path": str(output_path),
            "content_type": payload.media_type or "",
            "byte_size": payload.content_length,
            "sha256": payload.sha256,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "status": "complete",
            "metadata": {"cached": False, "final_url": payload.final_url, **document.metadata},
        }


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(partial, path)


class CninfoAcquisitionRunner:
    def __init__(self, config: GlobalAcquisitionConfig) -> None:
        self.config = config
        self.settings = require_proxy(Settings())
        self.definition = PatchRegistry().get(CNINFO_SOURCE)

    def run_batch(self) -> dict[str, Any]:
        self.config.raw_root.mkdir(parents=True, exist_ok=True)
        counts = {"issuers": 0, "filings": 0, "documents": 0, "errors": 0}
        with (
            GlobalSourceAcquisitionState(self.config.database_url, CNINFO_SOURCE) as state,
            PostgresAcquisitionState(self.config.database_url, CNINFO_SOURCE) as queue,
            PoliteHttpClient(self.settings) as client,
        ):
            state.ensure_source(self.definition)
            queue.ensure_scan_years(self.config.target_years)
            year = queue.active_backfill_year(self.config.target_years) or self.config.target_years[0]
            run_id = datetime.now(UTC).strftime(f"cninfo-{year}-%Y%m%dT%H%M%SZ")
            queue.begin_run(run_id, year, "backfill")
            adapter = PatchRegistry().create_adapter(CNINFO_SOURCE, self.settings, client)
            issuers = queue.claim_issuers(self.config.cninfo_issuer_limit, filing_year=year)
            for issuer in issuers:
                counts["issuers"] += 1
                try:
                    result = self._acquire_issuer(adapter, state, client, issuer, year)
                    counts["filings"] += result["filings"]
                    counts["documents"] += result["documents"]
                    queue.complete_issuer(issuer.cik, filing_year=year)
                except Exception as exc:  # noqa: BLE001
                    counts["errors"] += 1
                    queue.fail_issuer(
                        issuer.cik,
                        f"{type(exc).__name__}: {exc}",
                        filing_year=year,
                    )
            queue.finish_run(
                run_id,
                "complete" if counts["errors"] == 0 else "partial",
                counts,
            )
            return {"source_id": CNINFO_SOURCE, "target_year": year, "counts": counts, "state": state.stats()}

    def _acquire_issuer(
        self,
        adapter: Any,
        state: GlobalSourceAcquisitionState,
        client: PoliteHttpClient,
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
        filings = list(
            adapter.list_filings(
                entity,
                begin=date(year, 1, 1),
                end=date(year, 12, 31),
                category=CninfoAdapter.FINANCIAL_REPORT_CATEGORIES,
                max_pages=5,
            )
        )
        filings = [row for row in filings if "摘要" not in (row.title or "")]
        unique = {row.filing_id: row for row in filings}
        state.upsert_filings(unique.values(), self.config.raw_root)
        documents = 0
        for filing in unique.values():
            documents += self._acquire_filing(adapter, state, client, filing)
        return {"filings": len(unique), "documents": documents}

    def _acquire_filing(
        self,
        adapter: Any,
        state: GlobalSourceAcquisitionState,
        client: PoliteHttpClient,
        filing: FilingRef,
    ) -> int:
        local_dir = self.config.raw_root / CNINFO_SOURCE / str(filing.filed_at.year) / filing.source_entity_id / filing.filing_id
        manifest: list[dict[str, Any]] = []
        try:
            for document in adapter.list_documents(filing):
                path = local_dir / safe_filename(document.filename, f"{filing.filing_id}.pdf")
                result = download_document(client, document, path)
                state.upsert_document(filing.filing_id, document.document_type or "primary", result)
                manifest.append(result)
            write_manifest(local_dir / "filing.json", {"filing": filing.model_dump(mode="json"), "documents": manifest})
            state.complete_filing(filing.filing_id)
        except Exception as exc:
            state.fail_filing(filing.filing_id, f"{type(exc).__name__}: {exc}")
            raise
        return len(manifest)


class EsefAcquisitionRunner:
    def __init__(self, config: GlobalAcquisitionConfig) -> None:
        self.config = config
        self.settings = require_proxy(Settings())
        self.definition = PatchRegistry().get(ESEF_SOURCE)

    def run_batch(self) -> dict[str, Any]:
        counts = {"discovered": 0, "filings": 0, "documents": 0, "errors": 0}
        with (
            GlobalSourceAcquisitionState(self.config.database_url, ESEF_SOURCE) as state,
            PoliteHttpClient(self.settings) as client,
        ):
            state.ensure_source(self.definition)
            adapter = PatchRegistry().create_adapter(ESEF_SOURCE, self.settings, client)
            for year in self.config.target_years:
                counts["discovered"] += self._discover_year(adapter, state, year)
            claimed: list[dict[str, Any]] = []
            target_year = self.config.target_years[0]
            for year in self.config.target_years:
                claimed = state.claim_filings(year, self.config.esef_filing_limit)
                if claimed:
                    target_year = year
                    break
            for filing in claimed:
                counts["filings"] += 1
                try:
                    counts["documents"] += self._download_filing(state, client, filing)
                except Exception as exc:  # noqa: BLE001
                    counts["errors"] += 1
                    state.fail_filing(
                        filing["source_filing_id"], f"{type(exc).__name__}: {exc}"
                    )
            return {"source_id": ESEF_SOURCE, "target_year": target_year, "counts": counts, "state": state.stats()}

    def _discover_year(self, adapter: Any, state: GlobalSourceAcquisitionState, year: int) -> int:
        checkpoint = f"filing-index:{year}"
        if not state.checkpoint_due(checkpoint, self.config.discovery_refresh_hours):
            return 0
        state.begin_checkpoint(checkpoint, {"year": year})
        try:
            filings = list(
                adapter.list_recent_filings(
                    begin=date(year, 1, 1),
                    end=date(year, 12, 31),
                    page_size=200,
                    max_pages=20,
                )
            )
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
                    legal_name=str(filing.metadata.get("entity_name") or identifier),
                    jurisdiction=str(filing.metadata.get("country") or filing.metadata.get("discovery_country") or ""),
                    lei=identifier if len(identifier) == 20 else None,
                    metadata={"discovery_channel": "filings.xbrl.org"},
                )
                valid.append(filing)
            state.upsert_entities(entities.values())
            count = state.upsert_filings(valid, self.config.raw_root)
            state.complete_checkpoint(checkpoint, {"filings_discovered": count, "entities": len(entities)})
            return count
        except Exception as exc:
            state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")
            raise

    def _download_filing(
        self,
        state: GlobalSourceAcquisitionState,
        client: PoliteHttpClient,
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
        manifest: list[dict[str, Any]] = []
        try:
            for kind, url, media_type in candidates:
                if not url:
                    continue
                filename = safe_filename(str(url), f"{kind}.bin")
                document = DocumentRef(
                    source_id=ESEF_SOURCE,
                    document_id=f"{filing_id}:{kind}",
                    filing_id=filing_id,
                    entity_id=str(metadata.get("entity_id") or filing["source_issuer_id"]),
                    url=str(url),
                    filename=filename,
                    document_type=kind,
                    expected_media_type=media_type,
                    filed_at=filing["filing_date"],
                )
                result = download_document(client, document, local_dir / filename)
                state.upsert_document(filing_id, kind, result)
                manifest.append(result)
            write_manifest(local_dir / "filing.json", {"filing": filing, "documents": manifest})
            state.complete_filing(filing_id)
            return len(manifest)
        except Exception:
            raise


class GleifAcquisitionRunner:
    FILE_TYPES = ("lei2", "rr", "repex")

    def __init__(self, config: GlobalAcquisitionConfig) -> None:
        self.config = config
        self.settings = require_proxy(Settings())
        self.definition = SourceRegistry.load(
            Path(__file__).resolve().parents[2] / "config" / "global_sources_base.yaml"
        ).get(GLEIF_SOURCE)

    def run_batch(self) -> dict[str, Any]:
        checkpoint = "golden-copy:latest"
        with (
            GlobalSourceAcquisitionState(self.config.database_url, GLEIF_SOURCE) as state,
            PoliteHttpClient(self.settings) as client,
        ):
            state.ensure_source(self.definition)
            if not state.checkpoint_due(checkpoint, self.config.discovery_refresh_hours):
                return {"source_id": GLEIF_SOURCE, "status": "fresh", "state": state.stats()}
            state.begin_checkpoint(checkpoint)
            today = datetime.now(UTC).date()
            output_dir = self.config.raw_root / GLEIF_SOURCE / today.strftime("%Y/%m/%d")
            rows = []
            try:
                for file_type in self.FILE_TYPES:
                    url = GleifGoldenCopy.url(file_type=file_type, file_format="csv")
                    document = DocumentRef(
                        source_id=GLEIF_SOURCE,
                        document_id=f"{today}:{file_type}",
                        url=url,
                        filename=f"{file_type}.csv.zip",
                        document_type=f"GLEIF {file_type} Golden Copy",
                        expected_media_type="application/zip",
                    )
                    result = download_document(client, document, output_dir / document.filename)
                    result["metadata"] = {"file_type": file_type, "snapshot_date": today.isoformat()}
                    state.upsert_source_object(
                        f"{today}:{file_type}", f"golden-copy-{file_type}", result
                    )
                    rows.append(result)
                write_manifest(output_dir / "manifest.json", {"source_id": GLEIF_SOURCE, "objects": rows})
                state.complete_checkpoint(checkpoint, {"snapshot_date": today.isoformat(), "objects": len(rows)})
            except Exception as exc:
                state.fail_checkpoint(checkpoint, f"{type(exc).__name__}: {exc}")
                raise
            return {"source_id": GLEIF_SOURCE, "status": "complete", "objects": len(rows), "state": state.stats()}


def run_source(source_id: str, config: GlobalAcquisitionConfig) -> dict[str, Any]:
    runners = {
        CNINFO_SOURCE: CninfoAcquisitionRunner,
        ESEF_SOURCE: EsefAcquisitionRunner,
        GLEIF_SOURCE: GleifAcquisitionRunner,
    }
    try:
        runner_class = runners[source_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported source {source_id!r}; choose from {SUPPORTED_SOURCES}") from exc
    return runner_class(config).run_batch()


def source_status(source_id: str, config: GlobalAcquisitionConfig) -> dict[str, Any]:
    with GlobalSourceAcquisitionState(config.database_url, source_id) as state:
        return {"source_id": source_id, **state.stats()}
