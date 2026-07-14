from valuechain.companies_house_bulk_acquisition import parse_accounts_bulk_index


def test_accounts_bulk_index_parses_unique_daily_zips_recent_first() -> None:
    html = """
    <a href="Accounts_Bulk_Data-2026-07-13.zip">old</a>
    <a href="/Accounts_Bulk_Data-2026-07-14.zip">latest</a>
    <a href="Accounts_Bulk_Data-2026-07-14.zip">duplicate</a>
    <a href="BasicCompanyDataAsOneFile-2026-07-01.zip">not accounts</a>
    """

    rows = parse_accounts_bulk_index(
        html, "https://download.companieshouse.gov.uk/en_accountsdata.html"
    )

    assert [row.effective_date.isoformat() for row in rows] == [
        "2026-07-14",
        "2026-07-13",
    ]
    assert rows[0].object_key == "daily-accounts:2026-07-14"
    assert rows[0].url == (
        "https://download.companieshouse.gov.uk/Accounts_Bulk_Data-2026-07-14.zip"
    )


def test_accounts_bulk_index_ignores_unrelated_links() -> None:
    assert parse_accounts_bulk_index("<a href='readme.html'>readme</a>") == []
