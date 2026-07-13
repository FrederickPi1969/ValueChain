from __future__ import annotations

import importlib
from pathlib import Path

import yaml

from gcu_priority_markets.catalog import load_contracts, load_overlay, load_priority_markets
from gcu_priority_markets.registry import PatchRegistry, merge_source_catalog


def test_priority_market_scope_has_fifteen_missing_markets() -> None:
    markets = load_priority_markets()
    assert len(markets) == 15
    assert {row["jurisdiction"] for row in markets} == {
        "CN",
        "IN",
        "KR",
        "GB",
        "CA",
        "DE",
        "FR",
        "IT",
        "ES",
        "NL",
        "MX",
        "ID",
        "SA",
        "CH",
        "SG",
    }


def test_overlay_sources_validate_and_have_contracts() -> None:
    sources = load_overlay()
    contracts = load_contracts()
    assert len(sources) == 21
    for source in sources:
        assert source.source_id in contracts or source.source_id in {
            "priority_eu_esef",
            "fca_firds_priority",
            "tmx_issuer_lists",
        }


def test_all_adapter_import_paths_resolve() -> None:
    for source in load_overlay():
        module_name, class_name = source.adapter.rsplit(":", 1)
        module = importlib.import_module(module_name)
        assert getattr(module, class_name)


def test_patch_registry_has_unique_sources() -> None:
    registry = PatchRegistry()
    ids = [source.source_id for source in registry.all()]
    assert len(ids) == len(set(ids))
    assert registry.get("cninfo").jurisdictions == ["CN"]


def test_merge_catalog_replaces_and_adds_without_mutating_base(tmp_path: Path) -> None:
    base = tmp_path / "sources.yaml"
    output = tmp_path / "merged.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "source_id": "cninfo",
                        "name": "placeholder",
                        "jurisdictions": ["CN"],
                        "adapter": "gcu.adapters.official_web:OfficialWebAdapter",
                        "access_mode": "official_web",
                        "official_url": "https://example.invalid",
                    },
                    {
                        "source_id": "sec_edgar",
                        "name": "keep",
                        "jurisdictions": ["US"],
                        "adapter": "x:y",
                        "access_mode": "public_api",
                        "official_url": "https://www.sec.gov",
                    },
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    before = base.read_bytes()
    report = merge_source_catalog(base_path=base, output_path=output)
    assert base.read_bytes() == before
    assert "cninfo" in report["replaced"]
    assert "fca_firds_priority" in report["added"]
    merged = yaml.safe_load(output.read_text(encoding="utf-8"))["sources"]
    sec = next(row for row in merged if row["source_id"] == "sec_edgar")
    cn = next(row for row in merged if row["source_id"] == "cninfo")
    assert sec["name"] == "keep"
    assert cn["adapter"].startswith("gcu_priority_markets")
