from valuechain.acquisition_state import AcquisitionIssuer, AcquisitionState


def issuer(cik: str, ticker: str, priority: int) -> AcquisitionIssuer:
    return AcquisitionIssuer(
        cik=cik,
        ticker=ticker,
        company_name=f"{ticker} Inc.",
        exchange="NASDAQ",
        priority=priority,
    )


def test_acquisition_state_claims_priority_and_persists_completion(tmp_path) -> None:
    state_path = tmp_path / "acquisition.sqlite3"
    with AcquisitionState(state_path) as state:
        state.upsert_issuers(
            [
                issuer("0000000002", "SECOND", 100),
                issuer("0000000001", "FIRST", 0),
            ]
        )
        state.ensure_scan_years((2026, 2025))
        assert state.active_backfill_year((2026, 2025)) == 2026
        claimed = state.claim_issuers(limit=1)
        assert [row.ticker for row in claimed] == ["FIRST"]
        state.complete_issuer(claimed[0].cik)
        assert state.stats()["issuers"] == {"complete": 1, "pending": 1}

    with AcquisitionState(state_path) as reopened:
        claimed = reopened.claim_issuers(limit=1)
        assert [row.ticker for row in claimed] == ["SECOND"]
        reopened.complete_issuer(claimed[0].cik, filing_year=2026)
        assert reopened.active_backfill_year((2026, 2025)) == 2025
        assert reopened.year_progress(2025) == {"pending": 2}


def test_running_issuer_is_recovered_after_interrupted_process(tmp_path) -> None:
    with AcquisitionState(tmp_path / "state.sqlite3") as state:
        state.upsert_issuers(
            [issuer("0000000001", "FIRST", 0), issuer("0000000002", "SECOND", 1)]
        )
        state.ensure_scan_years((2026,))
        assert [row.ticker for row in state.claim_issuers(limit=1)] == ["FIRST"]
        assert [row.ticker for row in state.claim_issuers(limit=1)] == ["FIRST"]


def test_retry_year_does_not_block_older_pending_backfill(tmp_path) -> None:
    with AcquisitionState(tmp_path / "state.sqlite3") as state:
        state.upsert_issuers(
            [issuer("0000000001", "FIRST", 0), issuer("0000000002", "SECOND", 1)]
        )
        state.ensure_scan_years((2026, 2025))

        first = state.claim_issuers(limit=1, filing_year=2026)[0]
        state.fail_issuer(first.cik, "temporary upstream error", filing_year=2026)
        second = state.claim_issuers(limit=1, filing_year=2026)[0]
        state.complete_issuer(second.cik, filing_year=2026)

        assert state.year_progress(2026) == {"complete": 1, "retry": 1}
        assert state.active_backfill_year((2026, 2025)) == 2025
