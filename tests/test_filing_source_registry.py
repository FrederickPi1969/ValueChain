from pathlib import Path

import yaml


REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config" / "filing_sources_2026.yaml"


def test_filing_source_registry_has_unique_ranked_sources() -> None:
    payload = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    sources = payload["sources"]
    source_ids = [source["source_id"] for source in sources]

    assert payload["download_policy"]["start_date"] == "2026-01-01"
    assert source_ids[0] == "sec_edgar"
    assert len(source_ids) == len(set(source_ids))
    assert all(isinstance(source["rank"], int) for source in sources)
    assert all(source["jurisdictions"] for source in sources)
    assert all("repo_status" in source for source in sources)
    assert all("missing" in source for source in sources)


def test_us_is_the_only_first_wave_market() -> None:
    payload = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    first_wave = [source for source in payload["sources"] if source["wave"] == "us_2026"]

    assert [source["source_id"] for source in first_wave] == ["sec_edgar"]
    assert first_wave[0]["jurisdictions"] == ["US"]
