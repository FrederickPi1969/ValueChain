from valuechain.relation_llm import normalize_object_payload


def test_normalize_object_payload_accepts_structured_llm_object() -> None:
    assert normalize_object_payload({"name": "Customer", "type": "Generic"}) == "Customer"


def test_normalize_object_payload_rejects_empty_structured_object() -> None:
    assert normalize_object_payload({"type": "Generic"}) == ""
