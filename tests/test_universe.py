from pathlib import Path

from valuechain.universe import parse_csv_arg, parse_tickers, read_universe, summarize_universe


def test_parse_tickers_normalizes_comma_separated_list() -> None:
    assert parse_tickers("nvda, AMD, msft") == ["NVDA", "AMD", "MSFT"]


def test_parse_csv_arg_keeps_role_case_free_values() -> None:
    assert parse_csv_arg("foundry, cloud_hyperscaler") == ["foundry", "cloud_hyperscaler"]


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


def test_read_universe_filters_roles_priority_and_limit(tmp_path: Path) -> None:
    csv_path = tmp_path / "universe.csv"
    csv_path.write_text(
        "ticker,company_name,role,priority,notes\n"
        "NVDA,NVIDIA Corporation,accelerator_compute,1,gpu\n"
        "MSFT,Microsoft Corporation,cloud_hyperscaler,1,cloud\n"
        "IBM,International Business Machines Corporation,enterprise_ai_cloud,3,cloud\n",
        encoding="utf-8",
    )
    companies = read_universe(
        csv_path,
        roles=["cloud_hyperscaler", "enterprise_ai_cloud"],
        max_priority=2,
        limit=1,
    )
    assert [company.ticker for company in companies] == ["MSFT"]
    summary = summarize_universe(companies)
    assert summary["role_counts"] == {"cloud_hyperscaler": 1}
