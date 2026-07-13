from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

from gcu.config import Settings
from gcu.http import PoliteHttpClient
from gcu.models import SourceDefinition

from gcu_priority_markets.catalog import load_contracts, load_overlay


class PatchRegistry:
    """Loads only patch sources and injects their declarative source contract."""

    def __init__(
        self,
        sources: list[SourceDefinition] | None = None,
        contracts: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        source_list = sources or load_overlay()
        self._sources = {source.source_id: source for source in source_list}
        if len(self._sources) != len(source_list):
            raise ValueError("Patch source_id values must be unique")
        self._contracts = contracts or load_contracts()

    def all(self) -> list[SourceDefinition]:
        return sorted(self._sources.values(), key=lambda item: item.source_id)

    def get(self, source_id: str) -> SourceDefinition:
        try:
            return self._sources[source_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._sources))
            raise KeyError(f"Unknown patch source_id={source_id!r}. Known sources: {known}") from exc

    def contract(self, source_id: str) -> dict[str, Any]:
        return self._contracts.get(source_id, {})

    def create_adapter(
        self,
        source_id: str,
        settings: Settings,
        client: PoliteHttpClient,
        **kwargs: Any,
    ) -> Any:
        definition = self.get(source_id)
        module_name, class_name = definition.adapter.rsplit(":", 1)
        module = importlib.import_module(module_name)
        adapter_class = getattr(module, class_name)
        contract = kwargs.pop("contract", self.contract(source_id))
        return adapter_class(
            definition=definition,
            settings=settings,
            client=client,
            contract=contract,
            **kwargs,
        )


def merge_source_catalog(
    *,
    base_path: Path,
    output_path: Path,
    overlay: list[SourceDefinition] | None = None,
) -> dict[str, Any]:
    """Create a merged catalog without modifying the deployed catalog in-place."""

    payload = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    base_rows = payload.get("sources", [])
    if not isinstance(base_rows, list):
        raise ValueError("Base source catalog must contain a list at key 'sources'")

    patch_sources = overlay or load_overlay()
    patch_rows = {
        item.source_id: item.model_dump(mode="json", exclude_none=True) for item in patch_sources
    }
    output_rows: list[dict[str, Any]] = []
    replaced: list[str] = []
    existing_ids: set[str] = set()

    for raw in base_rows:
        source_id = str(raw.get("source_id", ""))
        existing_ids.add(source_id)
        if source_id in patch_rows:
            output_rows.append(patch_rows[source_id])
            replaced.append(source_id)
        else:
            output_rows.append(raw)

    added = sorted(set(patch_rows) - existing_ids)
    output_rows.extend(patch_rows[source_id] for source_id in added)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(
            {"sources": output_rows},
            sort_keys=False,
            allow_unicode=True,
            width=120,
        ),
        encoding="utf-8",
    )
    return {
        "base_path": str(base_path),
        "output_path": str(output_path),
        "base_count": len(base_rows),
        "patch_count": len(patch_rows),
        "replaced": sorted(replaced),
        "added": added,
        "merged_count": len(output_rows),
    }
