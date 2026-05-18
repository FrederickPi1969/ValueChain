from valuechain.models import Company
from valuechain.planning import build_execution_plan


def test_execution_plan_estimates_industry_batch_size() -> None:
    companies = [
        Company("NVDA", "NVIDIA Corporation", role="accelerator_compute", priority=1),
        Company("MSFT", "Microsoft Corporation", role="cloud_hyperscaler", priority=1),
    ]
    plan = build_execution_plan(
        companies=companies,
        forms=("10-K", "10-Q"),
        max_filings_per_company=3,
        filing_date_from="2025-01-01",
    ).to_dict()
    assert plan["company_count"] == 2
    assert plan["planned_filings_upper_bound"] == 6
    assert plan["estimated_sec_requests_upper_bound"] == 9
    assert plan["universe"]["role_counts"]["cloud_hyperscaler"] == 1
