from valuechain.curated_universe import load_curated_companies


def test_korea_watchlist_is_intentionally_bounded() -> None:
    companies = load_curated_companies("korea")

    assert 70 <= len(companies) <= 100
    assert len({company.ticker for company in companies}) == len(companies)
    assert {"005930", "000660", "005380"}.issubset(
        {company.ticker for company in companies}
    )


def test_japan_watchlist_is_intentionally_bounded() -> None:
    companies = load_curated_companies("japan")

    assert 70 <= len(companies) <= 100
    assert len({company.ticker for company in companies}) == len(companies)
    assert {"7203", "6758", "8035"}.issubset(
        {company.ticker for company in companies}
    )
