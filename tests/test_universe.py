from pathlib import Path

from valuechain.universe import parse_tickers, read_universe


def test_parse_tickers_normalizes_comma_separated_list() -> None:
    assert parse_tickers("nvda, AMD, msft") == ["NVDA", "AMD", "MSFT"]


def test_read_universe_filters_tickers(tmp_path: Path) -> None:
    csv_path = tmp_path / "universe.csv"
    csv_path.write_text(
        "ticker,company_name,role,priority,notes\n"
        "NVDA,NVIDIA Corporation,compute,1,gpu\n"
        "AMD,Advanced Micro Devices Inc.,compute,1,gpu\n",
        encoding="utf-8",
    )
    companies = read_universe(csv_path, ["AMD"])
    assert len(companies) == 1
    assert companies[0].ticker == "AMD"

