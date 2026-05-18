from valuechain.llm_client import LLMConfig, parse_json_content, resolve_from_report


def test_parse_json_content_accepts_fenced_json() -> None:
    assert parse_json_content("```json\n[{\"a\": 1}]\n```") == [{"a": 1}]


def test_parse_json_content_extracts_embedded_array() -> None:
    assert parse_json_content("answer:\n[{\"relation_type\":\"supplier_dependency\"}]") == [
        {"relation_type": "supplier_dependency"}
    ]


def test_resolve_from_report_maps_model_to_openai_base(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"Qwen/Qwen3.5-4B": ["127.0.0.1:18000"]}

    monkeypatch.setattr("valuechain.llm_client.httpx.get", lambda *args, **kwargs: Response())
    config = resolve_from_report(
        LLMConfig(
            base_url="",
            api_key="1969",
            model="Qwen/Qwen3.5-4B",
            report_url="http://report",
        )
    )
    assert config.base_url == "http://127.0.0.1:18000/v1"
