import importlib
from pathlib import Path

from gcu.registry import SourceRegistry
from gcu_priority_markets.cli import _source_definition
from valuechain.global_universe_store import (
    deduplicate_entities,
    file_sha256,
    read_entity_csv,
    read_filing_jsonl,
)
from gcu.models import EntityRef


def test_read_entity_csv_preserves_global_identifiers(tmp_path: Path) -> None:
    path = tmp_path / "entities.csv"
    path.write_text(
        "entity_id,source_id,source_entity_id,legal_name,jurisdiction,exchange,ticker,lei,isin,local_registry_id,aliases,metadata\n"
        'cninfo-a,cninfo,a,Example Co,CN,XSHG,600000,LEI1,CN0001,REG1,"[""Alias""]","{""market"":""SSE""}"\n',
        encoding="utf-8",
    )

    rows = read_entity_csv(path)

    assert len(rows) == 1
    assert rows[0].lei == "LEI1"
    assert rows[0].isin == "CN0001"
    assert rows[0].aliases == ["Alias"]
    assert rows[0].metadata["market"] == "SSE"


def test_read_filing_jsonl_can_override_source(tmp_path: Path) -> None:
    path = tmp_path / "filings.jsonl"
    path.write_text(
        '{"source_id":"old","filing_id":"1","entity_id":"e","source_entity_id":"i","filed_at":"2026-01-02"}\n',
        encoding="utf-8",
    )

    rows = read_filing_jsonl(path, source_id="cninfo")

    assert rows[0].source_id == "cninfo"


def test_file_sha256_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.csv"
    path.write_bytes(b"abc")

    assert file_sha256(path) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_source_definition_resolves_patch_and_base_catalogs() -> None:
    assert _source_definition("cninfo").source_id == "cninfo"
    assert _source_definition("jpx").source_id == "jpx"


def test_every_base_catalog_adapter_is_importable() -> None:
    for source in SourceRegistry.load().all():
        module_name, class_name = source.adapter.rsplit(":", 1)
        module = importlib.import_module(module_name)
        assert getattr(module, class_name)


def test_exact_source_key_duplicates_are_collapsed() -> None:
    rows = [
        EntityRef(
            entity_id="one",
            source_id="example",
            source_entity_id="1",
            legal_name="Example Inc.",
            ticker="EX",
        ),
        EntityRef(
            entity_id="one-copy",
            source_id="example",
            source_entity_id="1",
            legal_name="Example Inc.",
            ticker="EX",
        ),
    ]

    result = deduplicate_entities(rows)

    assert len(result) == 1
    assert result[0].metadata["duplicate_source_rows"] == 2


def test_conflicting_source_key_duplicates_are_rejected() -> None:
    rows = [
        EntityRef(
            entity_id="one",
            source_id="example",
            source_entity_id="1",
            legal_name="First Inc.",
        ),
        EntityRef(
            entity_id="two",
            source_id="example",
            source_entity_id="1",
            legal_name="Second Inc.",
        ),
    ]

    try:
        deduplicate_entities(rows)
    except ValueError as exc:
        assert "Conflicting entities share source key" in str(exc)
    else:
        raise AssertionError("Expected a source-key conflict")
