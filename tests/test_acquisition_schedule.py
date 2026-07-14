from __future__ import annotations

from valuechain.acquisition_schedule import (
    choose_issuer_scan_plan,
    years_with_current,
)


class FakeState:
    def __init__(self, backfill_year: int | None, incremental_due: bool) -> None:
        self.backfill_year = backfill_year
        self.incremental_due = incremental_due
        self.rescan_calls: list[tuple[int, int]] = []

    def active_backfill_year(self, _years: tuple[int, ...]) -> int | None:
        return self.backfill_year

    def rescan_due(self, filing_year: int, rescan_hours: int) -> bool:
        self.rescan_calls.append((filing_year, rescan_hours))
        return self.incremental_due


def test_years_with_current_rolls_forward_without_duplicates() -> None:
    assert years_with_current((2026, 2025, 2024), 2027) == (
        2027,
        2026,
        2025,
        2024,
    )
    assert years_with_current((2026, 2025), 2026) == (2026, 2025)


def test_current_year_backfill_finishes_before_incremental_rescans() -> None:
    state = FakeState(backfill_year=2026, incremental_due=True)
    plan = choose_issuer_scan_plan(
        state, years=(2026, 2025), current_year=2026, rescan_hours=24
    )
    assert (plan.filing_year, plan.mode, plan.rescan_hours) == (
        2026,
        "backfill",
        None,
    )
    assert state.rescan_calls == []


def test_daily_incremental_scan_preempts_historical_backfill() -> None:
    state = FakeState(backfill_year=2024, incremental_due=True)
    plan = choose_issuer_scan_plan(
        state, years=(2026, 2025, 2024), current_year=2026, rescan_hours=24
    )
    assert (plan.filing_year, plan.mode, plan.rescan_hours) == (
        2026,
        "incremental",
        24,
    )


def test_historical_backfill_resumes_when_incremental_is_fresh() -> None:
    state = FakeState(backfill_year=2024, incremental_due=False)
    plan = choose_issuer_scan_plan(
        state, years=(2026, 2025, 2024), current_year=2026, rescan_hours=24
    )
    assert (plan.filing_year, plan.mode, plan.rescan_hours) == (
        2024,
        "backfill",
        None,
    )


def test_completed_backfill_remains_in_incremental_mode() -> None:
    state = FakeState(backfill_year=None, incremental_due=False)
    plan = choose_issuer_scan_plan(
        state, years=(2026, 2025), current_year=2026, rescan_hours=24
    )
    assert (plan.filing_year, plan.mode, plan.rescan_hours) == (
        2026,
        "incremental",
        24,
    )
