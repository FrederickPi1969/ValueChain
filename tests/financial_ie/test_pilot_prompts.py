from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.pilot_prompts import normalize_profile, normalize_signals, quote_in_text


def test_quote_validation_tolerates_whitespace_and_ellipsis() -> None:
    assert quote_in_text(
        "We rely on ... a limited number of suppliers",
        "We rely on third parties and a limited number of suppliers for components.",
    )


def test_quote_validation_repairs_sec_number_unit_line_breaks() -> None:
    assert quote_in_text("sales represented 22% of revenue", "sales represented 22\n% of revenue")


def test_normalize_profile_rejects_out_of_taxonomy_values() -> None:
    chunk = DocumentChunk("c1", "We design and sell semiconductor products.", section_hint="business")
    profile = normalize_profile(
        {
            "business_summary": "A chip company.",
            "primary_industry": "made_up_industry",
            "strategic_domains": ["semiconductor_supply_chain", "made_up"],
            "value_chain_roles": ["component_supplier"],
            "strategic_importance": 9,
            "evidence": [{"chunk_id": "c1", "quote": "design and sell semiconductor products"}],
        },
        {"c1": chunk},
    )
    assert profile["primary_industry"] is None
    assert profile["strategic_domains"] == ["semiconductor_supply_chain"]
    assert profile["strategic_importance"] == 5
    assert profile["evidence_valid"] is True


def test_normalize_signals_marks_unsupported_quote_for_review() -> None:
    chunk = DocumentChunk("c1", "Revenue increased 20% in fiscal 2025.", section_hint="mdna")
    signals = normalize_signals(
        {
            "signals": [
                {
                    "category": "demand_and_revenue",
                    "headline": "Revenue increased",
                    "statement": "Revenue increased in 2025.",
                    "direction": "positive",
                    "modality": "historical_fact",
                    "significance": 4,
                    "confidence": 0.9,
                    "chunk_id": "c1",
                    "evidence_quote": "Revenue declined 20%",
                }
            ]
        },
        {"c1": chunk},
    )
    assert signals[0]["evidence_valid"] is False
    assert signals[0]["review_status"] == "needs_evidence_review"


def test_normalize_signals_accepts_top_level_array() -> None:
    chunk = DocumentChunk("c1", "Revenue increased 20% in fiscal 2025.", section_hint="mdna")
    signals = normalize_signals(
        [
            {
                "category": "demand_and_revenue",
                "headline": "Revenue increased",
                "statement": "Revenue increased in 2025.",
                "chunk_id": "c1",
                "evidence_quote": "Revenue increased 20% in fiscal 2025.",
            }
        ],
        {"c1": chunk},
    )
    assert len(signals) == 1
    assert signals[0]["evidence_valid"] is True
