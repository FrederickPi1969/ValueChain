from __future__ import annotations

import csv
from pathlib import Path

from gcu_priority_markets.alerts import read_sedar_alerts
from gcu_priority_markets.tiering import assign_tiers, read_csv, tier_csv


def test_sedar_email_alert_parser(fixture_dir: Path) -> None:
    events = read_sedar_alerts(fixture_dir / "sedar_alert.eml")
    assert len(events) == 1
    event = events[0]
    assert event.issuer_name == "Example Mining Ltd."
    assert event.form == "Annual financial statements"
    assert event.detail_url.startswith("https://www.sedarplus.ca/")


def test_tiering_preserves_denominator(fixture_dir: Path, tmp_path: Path) -> None:
    output = tmp_path / "tiered.csv"
    report = tier_csv(fixture_dir / "tier_input.csv", output)
    assert report["input_rows"] == report["output_rows"] == 5
    rows = read_csv(output)
    assert {row["tier"] for row in rows} <= {"Tier 1", "Tier 2", "Tier 3"}
    assert rows[0]["tier"] in {"Tier 1", "Tier 2"}


def test_tiering_rejects_bad_weight_sum() -> None:
    rows = [{"entity_id": "A", "jurisdiction": "CN"}]
    try:
        assign_tiers(rows, weights={"market_cap": 0.9})
    except ValueError as exc:
        assert "sum" in str(exc)
    else:
        raise AssertionError("Expected invalid weights to fail")


def test_tiering_assigns_equal_percentiles_to_ties() -> None:
    rows = [
        {"entity_id": "A", "jurisdiction": "US", "market_cap_usd": "10"},
        {"entity_id": "B", "jurisdiction": "US", "market_cap_usd": "10"},
        {"entity_id": "C", "jurisdiction": "US", "market_cap_usd": "20"},
    ]

    tiered = assign_tiers(rows)

    assert tiered[0]["market_cap_percentile"] == tiered[1]["market_cap_percentile"]
