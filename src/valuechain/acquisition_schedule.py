from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class IssuerScanState(Protocol):
    def active_backfill_year(self, years: tuple[int, ...]) -> int | None: ...

    def rescan_due(self, filing_year: int, rescan_hours: int) -> bool: ...


@dataclass(frozen=True)
class IssuerScanPlan:
    filing_year: int
    mode: str
    rescan_hours: int | None


def years_with_current(target_years: tuple[int, ...], current_year: int) -> tuple[int, ...]:
    """Keep configured priority while ensuring a new calendar year is never omitted."""
    return tuple(dict.fromkeys((current_year, *target_years)))


def choose_issuer_scan_plan(
    state: IssuerScanState,
    *,
    years: tuple[int, ...],
    current_year: int,
    rescan_hours: int,
) -> IssuerScanPlan:
    """Interleave a daily current-year sweep with descending historical backfill."""
    backfill_year = state.active_backfill_year(years)
    if backfill_year is None:
        return IssuerScanPlan(current_year, "incremental", rescan_hours)
    if backfill_year != current_year and state.rescan_due(
        current_year, rescan_hours
    ):
        return IssuerScanPlan(current_year, "incremental", rescan_hours)
    return IssuerScanPlan(backfill_year, "backfill", None)

