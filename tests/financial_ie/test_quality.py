from valuechain.financial_ie.quality import audit_company_record


def base_record() -> dict:
    return {
        "status": "complete",
        "ticker": "ACME",
        "company_name": "Acme",
        "accession_number": "1",
        "profile": {"strategic_importance": 3, "evidence_valid": True},
        "financial_facts": [
            {"field": "revenue", "value": "100", "period_type": "duration", "period_start": "2025-01-01", "period_end": "2025-12-31"},
            {"field": "net_income", "value": "10", "period_type": "duration", "period_start": "2025-01-01", "period_end": "2025-12-31"},
            {"field": "operating_cash_flow", "value": "12", "period_type": "duration", "period_start": "2025-01-01", "period_end": "2025-12-31"},
            {"field": "total_assets", "value": "100", "period_type": "instant"},
            {"field": "total_liabilities", "value": "60", "period_type": "instant"},
            {"field": "stockholders_equity", "value": "40", "period_type": "instant"},
        ],
        "material_signals": [],
        "diagnostics": {},
    }


def test_quality_audit_accepts_consistent_core_financial_facts() -> None:
    assert audit_company_record(base_record()) == []


def test_quality_audit_flags_accounting_identity_mismatch() -> None:
    record = base_record()
    record["financial_facts"][-1]["value"] = "10"
    issues = audit_company_record(record)
    assert "accounting_identity_mismatch" in {issue["issue_type"] for issue in issues}


def test_quality_audit_allows_negative_stockholders_equity() -> None:
    record = base_record()
    record["financial_facts"][-1]["value"] = "-10"
    issues = audit_company_record(record)
    assert "negative_balance_sheet_fact" not in {issue["issue_type"] for issue in issues}


def test_quality_audit_flags_high_significance_unverified_and_modality_conflict() -> None:
    record = base_record()
    record["material_signals"] = [
        {
            "category": "capacity_and_supply",
            "headline": "Supply may fail",
            "significance": 5,
            "evidence_valid": False,
            "modality": "current_fact",
            "evidence_quote": "Supply may be disrupted.",
        }
    ]
    issues = audit_company_record(record)
    assert {issue["issue_type"] for issue in issues} >= {
        "signal_evidence_unverified",
        "current_fact_contains_risk_language",
    }
