import csv
from pathlib import Path

from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.pilot import (
    PilotRunConfig,
    evidence_failure_reason,
    materialize_audit_outputs,
    select_profile_chunks,
    select_signal_chunks,
    write_profile_review_sample,
    write_review_sample,
)


def test_profile_chunk_selection_prefers_business_and_products() -> None:
    chunks = [
        DocumentChunk("risk", "We may face generic risks.", section_hint="item_1a_risk_factors"),
        DocumentChunk(
            "business",
            "We design semiconductor accelerators and sell systems to cloud service providers.",
            section_hint="item_1_business",
        ),
    ]
    selected = select_profile_chunks(chunks)
    assert "business" in {chunk.chunk_id for chunk in selected}


def test_signal_selection_preserves_high_relevance_dependency() -> None:
    chunks = [
        DocumentChunk("generic", "Our business is competitive.", section_hint="item_1_business"),
        DocumentChunk(
            "dependency",
            "We rely on a sole-source supplier for critical components.",
            section_hint="item_1a_risk_factors",
        ),
    ]
    selected = select_signal_chunks(chunks, {"generic": 0.0, "dependency": 8.0})
    assert "dependency" in {chunk.chunk_id for chunk in selected}


def test_signal_review_sample_balances_failed_and_valid_evidence(tmp_path: Path) -> None:
    signals = [
        {"ticker": f"F{index}", "significance": 5, "evidence_valid": False}
        for index in range(3)
    ] + [
        {"ticker": f"V{index}", "significance": 5, "evidence_valid": True}
        for index in range(3)
    ]
    path = tmp_path / "review.csv"
    write_review_sample(path, signals, limit=4)
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["review_bucket"] for row in rows] == [
        "evidence_failed",
        "evidence_failed",
        "evidence_valid",
        "evidence_valid",
    ]


def test_profile_review_sample_writes_one_complete_company(tmp_path: Path) -> None:
    path = tmp_path / "profiles.csv"
    write_profile_review_sample(
        path,
        [
            {
                "status": "complete",
                "ticker": "ACME",
                "company_name": "Acme",
                "profile": {
                    "primary_industry": "industrials_and_infrastructure",
                    "strategic_domains": ["industrial_automation"],
                    "value_chain_roles": ["manufacturer"],
                    "evidence": [{"quote": "We manufacture systems."}],
                },
            },
            {"status": "missing", "ticker": "MISS"},
        ],
    )
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["ticker"] == "ACME"


def test_evidence_failure_reason_distinguishes_unknown_chunk_and_quote_mismatch() -> None:
    chunks = {"known": "source text"}
    assert evidence_failure_reason(False, "missing", "quote", chunks) == "unknown_chunk_id"
    assert evidence_failure_reason(False, "known", "different", chunks) == "quote_not_found"
    assert evidence_failure_reason(True, "known", "source text", chunks) == ""


def test_materialized_summary_counts_company_coverage_and_failure_reasons(tmp_path: Path) -> None:
    record = {
        "status": "complete",
        "ticker": "ACME",
        "company_name": "Acme",
        "profile": {
            "primary_industry": "industrials_and_infrastructure",
            "evidence_valid": False,
            "evidence": [
                {
                    "quote": "modified quote",
                    "evidence_valid": False,
                    "evidence_failure_reason": "quote_not_found",
                }
            ],
        },
        "financial_facts": [
            {"field": "revenue", "value": "10"},
            {"field": "revenue", "value": "10"},
        ],
        "material_signals": [
            {
                "category": "demand_and_revenue",
                "headline": "Demand changed",
                "significance": 3,
                "evidence_valid": False,
                "evidence_failure_reason": "unknown_chunk_id",
            }
        ],
        "diagnostics": {},
    }
    summary = materialize_audit_outputs(
        tmp_path,
        [record],
        [record],
        PilotRunConfig(output_dir=tmp_path, target_count=1),
    )
    assert summary["financial_fact_company_coverage"]["revenue"] == 1
    assert summary["signal_evidence_failure_reasons"] == {"unknown_chunk_id": 1}
    assert summary["profile_evidence_failure_reasons"] == {"quote_not_found": 1}
    assert summary["database_writes"] == 0
