from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from gcu.config import Settings
from gcu.models import EntityRef
from gcu.registry import SourceRegistry
from gcu_priority_markets.adapters.official_export import OfficialExportAdapter
from gcu_priority_markets.catalog import load_contracts
from gcu_priority_markets.io import read_tabular_path
from valuechain.acquisition_schema import AcquisitionSchemaGuard
from valuechain.async_http import sha256_file
from valuechain.global_acquisition import (
    OFFICIAL_IMPORT_SOURCES,
    GlobalAcquisitionConfig,
    write_manifest,
)
from valuechain.global_acquisition_state import GlobalSourceAcquisitionState


TABULAR_SUFFIXES = {".csv", ".tsv", ".xls", ".xlsx", ".json", ".html", ".htm"}
YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


@dataclass(frozen=True)
class ImportPackage:
    source_id: str
    path: Path
    sha256: str
    byte_size: int
    modified_at: datetime

    @property
    def object_key(self) -> str:
        return f"official-package:{self.sha256}"

    @property
    def effective_date(self) -> date:
        years = [int(value) for value in YEAR_RE.findall(self.path.name)]
        year = max(years) if years else self.modified_at.year
        return date(year, 12, 31)


def discover_import_packages(root: Path, source_id: str) -> list[Path]:
    if source_id not in OFFICIAL_IMPORT_SOURCES:
        raise ValueError(f"Unsupported official import source: {source_id}")
    incoming = root / source_id / "incoming"
    if not incoming.exists():
        return []
    return sorted(
        (
            path
            for path in incoming.rglob("*")
            if path.is_file()
            and not path.name.endswith((".partial", ".manifest.json"))
        ),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )


def inspect_import_package(source_id: str, path: Path) -> ImportPackage:
    stat = path.stat()
    return ImportPackage(
        source_id=source_id,
        path=path,
        sha256=sha256_file(path),
        byte_size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
    )


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.partial")
    with source.open("rb") as input_handle, partial.open("wb") as output_handle:
        shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    os.replace(partial, destination)


class OfficialDisclosureImportRunner:
    """Ingest operator-owned official exports without scraping restricted portals."""

    def __init__(self, source_id: str, config: GlobalAcquisitionConfig) -> None:
        if source_id not in OFFICIAL_IMPORT_SOURCES:
            raise ValueError(f"Unsupported official import source: {source_id}")
        self.source_id = source_id
        self.config = config
        self.definition = SourceRegistry.load().get(source_id)
        self.schema_guard = AcquisitionSchemaGuard(config.database_url)
        self.adapter = OfficialExportAdapter(
            definition=self.definition,
            settings=Settings(),
            client=_NoNetworkClient(),
            contract=load_contracts().get(source_id, {}),
        )

    async def run_batch(self) -> dict[str, Any]:
        await self.schema_guard.prepare()
        incoming = self.config.official_import_root / self.source_id / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        paths = discover_import_packages(
            self.config.official_import_root, self.source_id
        )[: self.config.official_import_batch_limit]
        counts = {"packages": 0, "entities": 0, "filings": 0, "errors": 0}
        errors: list[str] = []
        with GlobalSourceAcquisitionState(
            self.config.database_url, self.source_id, False
        ) as state:
            state.ensure_source(self.definition)
        for path in paths:
            try:
                result = await __import__("asyncio").to_thread(
                    self._import_package, path
                )
                for key in ("packages", "entities", "filings"):
                    counts[key] += result[key]
            except Exception as exc:  # noqa: BLE001
                counts["errors"] += 1
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}"[:1000])
        return {
            "source_id": self.source_id,
            "status": (
                "partial"
                if counts["errors"]
                else "complete"
                if counts["packages"]
                else "awaiting_authorized_feed"
            ),
            "inbox": str(incoming),
            "counts": counts,
            "errors": errors,
        }

    def _import_package(self, path: Path) -> dict[str, int]:
        package = inspect_import_package(self.source_id, path)
        destination = (
            self.config.raw_root
            / self.source_id
            / "official-packages"
            / str(package.effective_date.year)
            / package.sha256[:2]
            / f"{package.sha256}.{path.name}"
        )
        if not destination.exists():
            _copy_atomic(path, destination)
        counts = {"packages": 1, "entities": 0, "filings": 0}
        with GlobalSourceAcquisitionState(
            self.config.database_url, self.source_id, False
        ) as state:
            state.upsert_source_object(
                package.object_key,
                "authorized-official-export",
                {
                    "source_url": f"authorized-import://{self.source_id}/{path.name}",
                    "local_path": str(destination),
                    "content_type": "application/octet-stream",
                    "byte_size": package.byte_size,
                    "sha256": package.sha256,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "status": "complete",
                    "metadata": {
                        "effective_date": package.effective_date.isoformat(),
                        "original_filename": path.name,
                        "original_modified_at": package.modified_at.isoformat(),
                        "access_mode": "operator_owned_official_export",
                    },
                },
            )
            if path.suffix.lower() in TABULAR_SUFFIXES:
                rows = read_tabular_path(destination)
                filings = list(self.adapter.parse_filings(rows))
                entities = {
                    row.source_entity_id: row
                    for row in self.adapter.parse_entities(rows)
                }
                entities.update(
                    {
                        row.source_entity_id: row
                        for row in self._entities_for_filings(filings)
                    }
                )
                counts["entities"] = state.upsert_entities(entities.values())
                counts["filings"] = state.upsert_filings(
                    filings, self.config.raw_root
                )

        write_manifest(
            destination.with_suffix(f"{destination.suffix}.manifest.json"),
            {
                "source_id": self.source_id,
                "object_key": package.object_key,
                "access_mode": "operator_owned_official_export",
                "original_path": str(path),
                "local_path": str(destination),
                "sha256": package.sha256,
                "byte_size": package.byte_size,
                "metadata_records": counts["filings"],
                "imported_at": datetime.now(UTC).isoformat(),
            },
        )
        processed = self.config.official_import_root / self.source_id / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        processed_path = processed / f"{package.sha256}.{path.name}"
        if not processed_path.exists():
            try:
                os.replace(path, processed_path)
            except OSError:
                _copy_atomic(path, processed_path)
                path.unlink()
        elif path.exists():
            path.unlink()
        return counts

    def _entities_for_filings(self, filings: list[Any]) -> list[EntityRef]:
        entities: dict[str, EntityRef] = {}
        for filing in filings:
            source_entity_id = filing.source_entity_id
            if not source_entity_id:
                continue
            name = str(filing.metadata.get("issuer_name") or source_entity_id)
            entities[source_entity_id] = EntityRef(
                entity_id=filing.entity_id,
                source_id=self.source_id,
                source_entity_id=source_entity_id,
                legal_name=name,
                jurisdiction=filing.metadata.get("jurisdiction"),
                ticker=filing.metadata.get("security_code"),
                metadata={"official_export_import": True},
            )
        return list(entities.values())


class _NoNetworkClient:
    """Satisfy adapter construction while making accidental network use explicit."""

    class _RateLimiter:
        def set_host_rate(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    rate_limiter = _RateLimiter()

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"Official import adapter cannot use network method {name}")
