import pytest

from valuechain.financial_ie.json_utils import parse_json_payload, recover_partial_object_array


def test_parse_json_payload_accepts_fenced_and_prefixed_json() -> None:
    assert parse_json_payload('```json\n{"answer": 3}\n```') == {"answer": 3}
    assert parse_json_payload('Result: [{"text":"Acme"}]') == [{"text": "Acme"}]


def test_parse_json_payload_rejects_unstructured_text() -> None:
    with pytest.raises(ValueError):
        parse_json_payload("no structured answer")


def test_recover_partial_object_array_keeps_only_complete_rows() -> None:
    content = '{"signals":[{"headline":"one"},{"headline":"two"},{"headline":"thr'
    assert recover_partial_object_array(content, "signals") == [
        {"headline": "one"},
        {"headline": "two"},
    ]
