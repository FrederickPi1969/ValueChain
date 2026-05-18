from valuechain.llm_client import parse_json_content


def test_parse_json_content_accepts_fenced_json() -> None:
    assert parse_json_content("```json\n[{\"a\": 1}]\n```") == [{"a": 1}]


def test_parse_json_content_extracts_embedded_array() -> None:
    assert parse_json_content("answer:\n[{\"relation_type\":\"supplier_dependency\"}]") == [
        {"relation_type": "supplier_dependency"}
    ]
