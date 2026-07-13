from datetime import date
from pathlib import Path

from gcu.adapters.sec_edgar import SecEdgarAdapter
from gcu.config import Settings
from gcu.http import PoliteHttpClient
from gcu.registry import SourceRegistry


def test_sec_adapter_is_constructible_from_migrated_catalog(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        database_path=tmp_path / "state.sqlite3",
    )
    registry = SourceRegistry.load()
    with PoliteHttpClient(settings) as client:
        adapter = registry.create_adapter("sec_edgar", settings, client)

        assert isinstance(adapter, SecEdgarAdapter)


def test_sec_master_index_reconciliation() -> None:
    text = (
        "Header\nCIK|Company Name|Form Type|Date Filed|Filename\n"
        "789019|MICROSOFT CORP|10-K|2026-01-01|"
        "edgar/data/789019/0000789019-26-000001.txt\n"
    )
    rows = list(SecEdgarAdapter.parse_master_index(text))
    result = SecEdgarAdapter.reconcile_accessions(
        rows, set(), cik="0000789019", forms={"10-K"}
    )

    assert result["missing_locally"] == ["0000789019-26-000001"]
    assert SecEdgarAdapter.quarter_for_day(date(2026, 7, 1)) == 3
