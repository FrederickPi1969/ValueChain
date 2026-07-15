import json
from pathlib import Path

from valuechain.financial_ie.pilot_sources import (
    filing_manifest_path,
    load_pilot_universe,
    issuer_search_terms,
    resolve_primary_document,
    select_exact_issuer,
)


def test_load_pilot_universe_reaches_100_unique_companies() -> None:
    rows = load_pilot_universe(Path("data/universe/ai_infra_universe.csv"))
    assert len(rows) == 100
    assert len({row["ticker"] for row in rows}) == 100
    assert {"NVDA", "AAPL", "LMT", "UPS"} <= {row["ticker"] for row in rows}


def test_filing_manifest_path_uses_hdd_partitioning() -> None:
    path = filing_manifest_path(
        {
            "filing_date": "2026-02-25",
            "source_issuer_id": "1045810",
            "source_filing_id": "0001045810-26-000021",
        },
        Path("/filings"),
    )
    assert path == Path(
        "/filings/2026/02/0001/0001045810/000104581026000021/filing.json"
    )


def test_resolve_primary_document_reads_download_manifest(tmp_path: Path) -> None:
    primary = tmp_path / "annual.htm"
    primary.write_text("filing", encoding="utf-8")
    manifest = tmp_path / "filing.json"
    manifest.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_kind": "primary_document",
                        "status": "complete",
                        "local_path": str(primary),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert resolve_primary_document(manifest) == primary


def test_select_exact_issuer_avoids_short_ticker_fuzzy_collision() -> None:
    rows = [
        {"ticker": "MUR", "company_name": "Murphy Oil"},
        {"ticker": "MU", "company_name": "Micron"},
    ]
    assert select_exact_issuer(rows, "MU") == rows[1]


def test_issuer_search_terms_remove_legal_suffixes_and_short_tokens() -> None:
    assert issuer_search_terms("ON Semiconductor Corporation") == ["Semiconductor"]
