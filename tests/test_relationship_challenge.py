from valuechain.relationship_challenge import challenge_payload, normalize_challenge


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
