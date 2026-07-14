from valuechain.cvm_bulk_acquisition import parse_cvm_bulk_index


def test_cvm_index_parses_versioned_target_years_recent_first() -> None:
    html = """
    <pre>
    <a href="dfp_cia_aberta_2024.zip">dfp_cia_aberta_2024.zip</a> 12-Jul-2026 07:22 13M
    <a href="dfp_cia_aberta_2025.zip">dfp_cia_aberta_2025.zip</a> 12-Jul-2026 07:17 12M
    <a href="dfp_cia_aberta_2026.zip">dfp_cia_aberta_2026.zip</a> 12-Jul-2026 07:13 190K
    </pre>
    """

    rows = parse_cvm_bulk_index(
        html,
        form="DFP",
        base_url="https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/",
        target_years=(2026, 2025),
    )

    assert [row.year for row in rows] == [2026, 2025]
    assert rows[0].object_key == "DFP:2026:20260712T0713"
    assert rows[0].effective_date.isoformat() == "2026-12-31"
    assert rows[0].advertised_size == "190K"


def test_cvm_index_rejects_wrong_form_and_missing_version_metadata() -> None:
    html = """
    <a href="itr_cia_aberta_2026.zip">wrong form</a> 12-Jul-2026 07:37 9M
    <a href="dfp_cia_aberta_2026.zip">missing metadata</a>
    """

    assert (
        parse_cvm_bulk_index(
            html,
            form="DFP",
            base_url="https://dados.cvm.gov.br/",
            target_years=(2026,),
        )
        == []
    )
