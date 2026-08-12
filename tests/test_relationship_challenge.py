from valuechain.relationship_challenge import (
    apply_explanation_rewrite, challenge_payload, normalize_challenge, validate_explanation,
)


def test_challenge_payload_is_limited_to_the_connection_and_supplied_evidence() -> None:
    payload = challenge_payload(
        {"source_entity_name": "TSMC", "target_entity_name": "NVIDIA", "relationship_type": "supplies_to"},
        [{"form": "10-K", "source_section": "Business", "evidence_text": "We use TSMC to make wafers.", "source_document_url": "https://sec.example"}],
        "Why is this shown?",
    )
    assert payload["displayed_connection"]["source"] == "TSMC"
    assert payload["evidence"][0]["text"] == "We use TSMC to make wafers."


def test_challenge_normalization_never_treats_unknown_model_labels_as_a_graph_change() -> None:
    result = normalize_challenge({"answer": "The sentence is ambiguous.", "assessment": "invented", "needs_reaudit": True})
    assert result["assessment"] == "inconclusive"
    assert result["needs_reaudit"] is True


def test_supported_verdict_detects_a_contradictory_legacy_explanation() -> None:
    result = normalize_challenge({
        "assessment": "supported",
        "answer": "The evidence does not support this connection. It is supported.",
        "supporting_facts": ["NVIDIA purchases memory from Micron."],
        "rationale": "Micron therefore supplies memory to NVIDIA.",
    })
    assert result["explanation_inconsistent"] is True
    assert result["explanation_consistency_reason"] == "supported_verdict_has_negative_conclusion"


def test_rewrite_keeps_assessment_immutable_and_falls_back_to_template_if_needed() -> None:
    challenge = normalize_challenge({
        "assessment": "supported",
        "answer": "The evidence does not support this.",
        "supporting_facts": ["NVIDIA purchases memory from Micron."],
        "rationale": "Micron therefore supplies memory to NVIDIA.",
    })
    repaired = apply_explanation_rewrite(challenge, {"answer": "Supported. NVIDIA purchases memory from Micron."})
    assert repaired["assessment"] == "supported"
    assert repaired["explanation_rewritten"] is True
    assert repaired["explanation_inconsistent"] is False
    assert validate_explanation(repaired["assessment"], repaired["answer"])["consistent"] is True


def test_concern_requires_a_clear_contradiction_or_evidence_gap() -> None:
    assert validate_explanation("concern", "Concern. The evidence contradicts the displayed direction.")["consistent"] is True
    assert validate_explanation("concern", "Concern. This relationship is interesting.")["consistent"] is False
