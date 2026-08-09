"""Single shared-state publisher for canonical relationship commands."""
from __future__ import annotations

from typing import Any

from valuechain.config import Settings
from valuechain.postgres import sync_canonical_layer_to_postgres, sync_relationship_audits_to_postgres, sync_relationship_lineage_to_postgres, sync_relationship_reviews_to_postgres


def publish_relationship_projection(settings: Settings, run_id: str, entities: list[dict[str, Any]], relationships: list[dict[str, Any]], lineage: list[dict[str, Any]], audits: list[dict[str, Any]] | None = None) -> None:
    """Publish one internally consistent relationship projection to Postgres."""
    sync_canonical_layer_to_postgres(settings.database_url, run_id, entities, relationships)
    sync_relationship_reviews_to_postgres(settings.database_url, run_id, relationships)
    if audits is not None:
        sync_relationship_audits_to_postgres(settings.database_url, run_id, audits)
    sync_relationship_lineage_to_postgres(settings.database_url, run_id, lineage)
