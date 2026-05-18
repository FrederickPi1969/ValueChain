from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from valuechain.models import Company
from valuechain.universe import summarize_universe


@dataclass(frozen=True)
class ExecutionPlan:
    companies: list[Company]
    forms: tuple[str, ...]
    max_filings_per_company: int
    filing_selection: str = "form_balanced"
    filing_date_from: str = ""
    filing_date_to: str = ""

    def to_dict(self) -> dict[str, Any]:
        filing_multiplier = len(self.forms) if self.filing_selection == "form_balanced" else 1
        planned_filings_upper_bound = len(self.companies) * self.max_filings_per_company * filing_multiplier
        # One SEC company ticker lookup, one submissions JSON per company, and
        # up to one archive document request per planned filing. Cached archive
        # files are skipped at runtime, so this is intentionally conservative.
        estimated_sec_requests = 1 + len(self.companies) + planned_filings_upper_bound
        summary = summarize_universe(self.companies)
        return {
            "company_count": len(self.companies),
            "forms": list(self.forms),
            "max_filings_per_company": self.max_filings_per_company,
            "filing_selection": self.filing_selection,
            "filing_date_from": self.filing_date_from,
            "filing_date_to": self.filing_date_to,
            "planned_filings_upper_bound": planned_filings_upper_bound,
            "estimated_sec_requests_upper_bound": estimated_sec_requests,
            "universe": summary,
            "companies": [company.to_dict() for company in self.companies],
        }


def build_execution_plan(
    companies: list[Company],
    forms: tuple[str, ...],
    max_filings_per_company: int,
    filing_selection: str = "form_balanced",
    filing_date_from: str = "",
    filing_date_to: str = "",
) -> ExecutionPlan:
    return ExecutionPlan(
        companies=companies,
        forms=forms,
        max_filings_per_company=max_filings_per_company,
        filing_selection=filing_selection,
        filing_date_from=filing_date_from,
        filing_date_to=filing_date_to,
    )
