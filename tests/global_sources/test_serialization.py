from __future__ import annotations

import csv
from pathlib import Path

from gcu.models import EntityRef

from gcu_priority_markets.serialization import ENTITY_FIELDS, write_models_csv


def test_model_csv_serializes_nested_fields(tmp_path: Path) -> None:
    path = tmp_path / "entities.csv"
    entity = EntityRef(
        entity_id="e1",
        source_id="test",
        source_entity_id="1",
        legal_name="Example",
        aliases=["Ex"],
        metadata={"a": 1},
    )
    assert write_models_csv(path, [entity], ENTITY_FIELDS) == 1
    with path.open("r", encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["legal_name"] == "Example"
    assert row["metadata"] == '{"a": 1}'
