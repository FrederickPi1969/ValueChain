from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

from gcu.config import Settings
from gcu.http import PoliteHttpClient
from gcu.models import SourceDefinition


def default_catalog_path() -> Path:
    package_root = Path(__file__).resolve().parents[2]
    candidates = [
        Path("config/global_sources_base.yaml"),
        package_root / "config/global_sources_base.yaml",
        Path("data/catalog/sources.yaml"),
        package_root / "data/catalog/sources.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Could not locate a global source catalog")


class SourceRegistry:
    def __init__(self, sources: list[SourceDefinition]) -> None:
        self._sources = {source.source_id: source for source in sources}
        if len(self._sources) != len(sources):
            raise ValueError("source_id values must be unique")

    @classmethod
    def load(cls, path: Path | None = None) -> SourceRegistry:
        catalog_path = path or default_catalog_path()
        with catalog_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
        sources = [SourceDefinition.model_validate(item) for item in payload["sources"]]
        return cls(sources)

    def all(self) -> list[SourceDefinition]:
        return sorted(self._sources.values(), key=lambda item: item.source_id)

    def get(self, source_id: str) -> SourceDefinition:
        try:
            return self._sources[source_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._sources))
            raise KeyError(f"Unknown source_id={source_id!r}. Known sources: {known}") from exc

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
        return adapter_class(definition=definition, settings=settings, client=client, **kwargs)
