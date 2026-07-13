from __future__ import annotations

from pathlib import Path

from gcu.config import Settings
from gcu.http import PoliteHttpClient

from gcu_priority_markets.registry import PatchRegistry


def _adapter(source_id: str):
    settings = Settings()
    client = PoliteHttpClient(settings)
    return client, PatchRegistry().create_adapter(source_id, settings, client)


def test_six_official_export_entities(fixture_dir: Path) -> None:
    client, adapter = _adapter("six_exchange")
    try:
        rows = list(adapter.list_entities(input_path=fixture_dir / "six_issuers.csv"))
    finally:
        client.close()
    assert len(rows) == 2
    assert rows[0].jurisdiction == "CH"
    assert rows[0].exchange == "SIX Swiss Exchange"


def test_nsm_official_export_filings(fixture_dir: Path) -> None:
    client, adapter = _adapter("fca_nsm")
    try:
        rows = list(adapter.list_filings(input_path=fixture_dir / "nsm_filings.csv"))
        documents = list(adapter.list_documents(rows[0]))
    finally:
        client.close()
    assert len(rows) == 1
    assert rows[0].filing_id == "NSM-2026-0001"
    assert rows[0].filed_at.isoformat() == "2026-07-10"
    assert len(documents) == 1
